# SI-assisted: written with Claude Code (Anthropic LLM) under the author's direction and reviewed by the author.
"""Step 5 - cleaning and aggregation: detections table -> cell x month table, wide per satellite.

Ported from the preparation project and adapted to the global data. Reads only
``data/processed/hotspots/`` (steps 3-4), never the raw CSVs.

Stages
  mask      static-source mask for NRT rows: 0.01 deg cells with SP detections of type 1/2/3
  coverage  observed, missing and low days per satellite x month (outages)
  cells     the cell x month table

Cleaning rules (config.SETTINGS, config.EXCLUSIONS)
  * Effective type in `calendar_types` (0 = vegetation fire). SP rows keep the FIRMS `type`. NRT rows have no
    `type`: they take the type of their 0.01 deg cell in the static mask, or 0 when the cell is not in it.
    Kept rows whose type was inferred are counted per cell-month (`n_type_inferred`), so the app can warn
    that the type of those detections was calculated and not delivered by FIRMS.
  * Confidence: MODIS >= `modis_min_confidence` (read from the raw value, so it can change without
    re-running the adapter), VIIRS nominal or high.
  * Not inside an exclusion (lava labelled as vegetation fire).
  * Latitude inside `calendar_lat_range` (false alarms in Antarctica and the high Arctic).
  FRP sums clip values below `frp_clip_min`. Counts are raw: outages are described by the coverage table.

Output
  cell_month_g<grid>.parquet   one row per (cell_lat, cell_lon, year, month) with at least one kept detection;
                               cell_lat / cell_lon are the SW corner; sorted by cell, then time
  cell_month_g<grid>.funnel.json   rows removed by each rule, per satellite and processing
  static_mask_d0.01.parquet, coverage_sat_month.parquet

Run from the repo root:
    .venv/Scripts/python -m pipeline.aggregate build            # all stages (about 3 min)
    .venv/Scripts/python -m pipeline.aggregate build --stages cells --grid 0.5
    .venv/Scripts/python -m pipeline.aggregate check
    .venv/Scripts/python -m pipeline.aggregate info
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import json
import shutil
import statistics
import time

from . import config

S = config.SETTINGS
SATS = {"terra": "Terra", "aqua": "Aqua", "snpp": "N", "n20": "N20", "n21": "N21"}  # column key -> satellite
INVENTORY_SAT = {"Terra": "Terra", "Aqua": "Aqua", "N": "SNPP", "N20": "N20", "N21": "N21"}
LOW_DAY_RATIO = 0.5  # same rule as the step-1 inventory: below 50 % of the centred 31-day median
STAGES = ["mask", "coverage", "cells"]


def log(msg: str) -> None:
    print(msg, flush=True)


def funnel_path(grid: float):
    return config.agg_path(grid).with_suffix(".funnel.json")


def mask_key(col: str) -> str:
    return f"CAST(round({col} * {round(1 / S.static_mask_deg)}) AS INTEGER)"


# ----------------------------------------------------------------------------- shared SQL
def effective_sql(where: str = "TRUE") -> str:
    """Detections with their effective type and confidence flag. SP keeps the FIRMS type; NRT inherits it."""
    hot = config.hotspots_sql()
    conf = ", ".join(f"'{c}'" for c in S.viirs_min_confidence)
    cols = (f"h.sensor, h.satellite, h.year, h.month, h.lat, h.lon, h.date, h.daynight, h.frp, h.processing, "
            f"CASE WHEN h.sensor = 'MODIS' THEN CAST(h.confidence_raw AS INTEGER) >= {S.modis_min_confidence} "
            f"ELSE h.confidence_norm IN ({conf}) END AS conf_ok")
    return f"""
        SELECT {cols}, h.type AS type_eff, false AS type_inferred
        FROM {hot} h WHERE h.processing = 'SP' AND ({where})
        UNION ALL
        SELECT {cols}, coalesce(m.type, 0) AS type_eff, true AS type_inferred
        FROM {hot} h LEFT JOIN static_mask m ON m.gy = {mask_key('h.lat')} AND m.gx = {mask_key('h.lon')}
        WHERE h.processing = 'NRT' AND ({where})"""


def type_ok_sql() -> str:
    return f"type_eff IN ({', '.join(str(t) for t in S.calendar_types)})"


def lat_ok_sql() -> str:
    lo, hi = S.calendar_lat_range
    return f"lat BETWEEN {lo} AND {hi}"


def keep_sql() -> str:
    return f"{type_ok_sql()} AND conf_ok AND NOT {config.exclusion_sql()} AND {lat_ok_sql()}"


def load_mask(con) -> None:
    path = config.static_mask_path()
    if not path.exists():
        raise SystemExit(f"Missing {path}: run `build --stages mask` first.")
    con.execute(f"CREATE OR REPLACE TEMP TABLE static_mask AS SELECT gy, gx, type FROM '{config.sql_path(path)}'")


# ----------------------------------------------------------------------------- stage: mask
def build_mask(con) -> None:
    t0 = time.time()
    path = config.static_mask_path()
    tmp = path.with_name(path.name + ".tmp")
    con.execute(f"""COPY (
        SELECT {mask_key('lat')} AS gy, {mask_key('lon')} AS gx,
               mode(type) AS type, CAST(count(*) AS INTEGER) AS n_static,
               CAST(count(DISTINCT sensor) AS TINYINT) AS n_sensors, min(date) AS first_date, max(date) AS last_date
        FROM {config.hotspots_sql()}
        WHERE processing = 'SP' AND type <> 0
        GROUP BY 1, 2 HAVING count(*) >= {S.static_mask_min_detections}
        ORDER BY 1, 2
    ) TO '{config.sql_path(tmp)}' (FORMAT parquet, COMPRESSION zstd)""")
    tmp.replace(path)
    rows = con.execute(f"SELECT type, count(*), sum(n_static) FROM '{config.sql_path(path)}' GROUP BY 1 ORDER BY 1").fetchall()
    log(f"mask: {sum(r[1] for r in rows):,} cells of {S.static_mask_deg} deg "
        f"({', '.join(f'type {t}: {n:,}' for t, n, _ in rows)}) -> {path.name} ({time.time() - t0:.0f}s)")


# ----------------------------------------------------------------------------- stage: coverage
def build_coverage(con) -> None:
    t0 = time.time()
    daily = con.execute(f"SELECT satellite, date, count(*) FROM {config.hotspots_sql()} GROUP BY ALL").fetchall()
    by_sat: dict[str, dict[dt.date, int]] = {}
    for sat, d, n in daily:
        by_sat.setdefault(sat, {})[d] = n
    rows = []
    for sat in SATS.values():
        counts = by_sat[sat]
        first, last = min(counts), max(counts)
        span = [first + dt.timedelta(days=i) for i in range((last - first).days + 1)]
        series = [counts.get(d, 0) for d in span]
        months: dict[tuple[int, int], list[int]] = {}
        for i, (d, c) in enumerate(zip(span, series)):
            med = statistics.median(series[max(0, i - 15): i + 16])
            low = 0 < c < LOW_DAY_RATIO * med
            m = months.setdefault((d.year, d.month), [0, 0, 0, 0])  # days in record, observed, missing, low
            m[0] += 1
            m[1] += c > 0
            m[2] += c == 0
            m[3] += low
        for (y, mo), (rec, obs, miss, low) in months.items():
            dim = calendar.monthrange(y, mo)[1]
            rows.append(f"('{sat}', {y}, {mo}, {dim}, {rec}, {obs}, {miss}, {low})")
    path = config.coverage_path()
    con.execute(f"""COPY (
        SELECT satellite, CAST(year AS SMALLINT) AS year, CAST(month AS TINYINT) AS month,
               CAST(days_in_month AS TINYINT) AS days_in_month, CAST(days_in_record AS TINYINT) AS days_in_record,
               CAST(obs_days AS TINYINT) AS obs_days, CAST(missing_days AS TINYINT) AS missing_days,
               CAST(low_days AS TINYINT) AS low_days, round(obs_days / days_in_month, 4) AS coverage
        FROM (VALUES {', '.join(rows)})
             AS t(satellite, year, month, days_in_month, days_in_record, obs_days, missing_days, low_days)
        ORDER BY satellite, year, month
    ) TO '{config.sql_path(path)}' (FORMAT parquet)""")
    summary = con.execute(f"""SELECT satellite, count(*), sum(missing_days), sum(low_days),
            count(*) FILTER (WHERE missing_days > 0), count(*) FILTER (WHERE missing_days >= 5)
        FROM '{config.sql_path(path)}' GROUP BY 1 ORDER BY 1""").fetchall()
    log(f"coverage: {len(rows)} satellite-months -> {path.name} ({time.time() - t0:.0f}s)")
    for sat, n, miss, low, m1, m5 in summary:
        log(f"   {sat:<6} {n} months, {miss} missing days, {low} low days; {m1} months with missing days ({m5} with >= 5)")


# ----------------------------------------------------------------------------- stage: cells
def cells_sql(grid: float, year: int) -> str:
    parts = []
    clip = S.frp_clip_min
    for key, sat in SATS.items():
        f = f"satellite = '{sat}'"
        for suffix, cond in (("", f), ("_d", f"{f} AND daynight = 'D'"), ("_n", f"{f} AND daynight = 'N'")):
            parts.append(f"CAST(count(*) FILTER ({cond}) AS INTEGER) AS n_{key}{suffix}")
        for suffix, cond in (("", f), ("_d", f"{f} AND daynight = 'D'"), ("_n", f"{f} AND daynight = 'N'")):
            parts.append(f"coalesce(sum(greatest(frp, {clip})) FILTER ({cond}), 0) AS frp_sum_{key}{suffix}")
    return f"""
        SELECT round(floor(lat / {grid}) * {grid}, 6) AS cell_lat, round(floor(lon / {grid}) * {grid}, 6) AS cell_lon,
               CAST(year AS SMALLINT) AS year, CAST(month AS TINYINT) AS month,
               {', '.join(parts)},
               CAST(count(*) FILTER (WHERE type_inferred) AS INTEGER) AS n_type_inferred
        FROM ({effective_sql(f'h.year = {year}')})
        WHERE {keep_sql()}
        GROUP BY ALL"""


def build_cells(con, grid: float) -> None:
    t0 = time.time()
    load_mask(con)
    out = config.agg_path(grid)
    parts_dir = config.TMP_DIR / f"{out.stem}_parts"
    shutil.rmtree(parts_dir, ignore_errors=True)
    parts_dir.mkdir(parents=True)
    years = [r[0] for r in con.execute(f"SELECT DISTINCT year FROM {config.hotspots_sql()} ORDER BY 1").fetchall()]
    for y in years:
        con.execute(f"COPY ({cells_sql(grid, y)}) TO '{config.sql_path(parts_dir / f'{y}.parquet')}' (FORMAT parquet)")
    t_years = time.time() - t0
    tmp = out.with_name(out.name + ".tmp")
    con.execute(f"""COPY (SELECT * FROM read_parquet('{config.sql_path(parts_dir)}/*.parquet')
                          ORDER BY cell_lat, cell_lon, year, month)
                    TO '{config.sql_path(tmp)}'
                    (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE {S.parquet_row_group_size})""")
    tmp.replace(out)
    shutil.rmtree(parts_dir, ignore_errors=True)
    n, cells = con.execute(f"SELECT count(*), count(DISTINCT (cell_lat, cell_lon)) FROM '{config.sql_path(out)}'").fetchone()
    log(f"cells: {n:,} cell-month rows, {cells:,} cells of {grid} deg, {out.stat().st_size / 1e6:.1f} MB -> {out.name} "
        f"({t_years:.0f}s per-year aggregation + {time.time() - t0 - t_years:.0f}s sort)")
    build_funnel(con, grid)


def build_funnel(con, grid: float) -> None:
    """Rows removed by each rule, in order: type -> confidence -> exclusions -> latitude."""
    t0 = time.time()
    type_ok, excl = type_ok_sql(), config.exclusion_sql()
    per_excl = ", ".join(
        f"count(*) FILTER (WHERE {type_ok} AND conf_ok AND {config.exclusion_sql_one(e)}) AS \"excl_{e.name}\""
        for e in config.EXCLUSIONS)
    cur = con.execute(f"""
        SELECT satellite, processing, count(*) AS total,
               count(*) FILTER (WHERE {type_ok}) AS after_type,
               count(*) FILTER (WHERE {type_ok} AND conf_ok) AS after_confidence,
               count(*) FILTER (WHERE {type_ok} AND conf_ok AND NOT {excl}) AS after_exclusions,
               count(*) FILTER (WHERE {keep_sql()}) AS kept,
               count(*) FILTER (WHERE type_inferred AND type_eff <> 0) AS nrt_inferred_static,
               count(*) FILTER (WHERE type_inferred AND {keep_sql()}) AS kept_type_inferred,
               count(*) FILTER (WHERE {keep_sql()} AND frp < {S.frp_clip_min}) AS kept_frp_clipped,
               {per_excl}
        FROM ({effective_sql()}) GROUP BY ALL ORDER BY ALL""")
    names = [d[0] for d in cur.description]
    rows = [dict(zip(names, r)) for r in cur.fetchall()]
    funnel_path(grid).write_text(json.dumps(rows, indent=1), encoding="utf-8")
    log(f"funnel -> {funnel_path(grid).name} ({time.time() - t0:.0f}s)")
    for r in rows:
        log(f"   {r['satellite']:<6} {r['processing']}: {r['total']:>12,} -> type {r['after_type']:>12,} -> confidence "
            f"{r['after_confidence']:>12,} -> exclusions {r['after_exclusions']:>12,} -> latitude {r['kept']:>12,} "
            f"({100 * r['kept'] / r['total']:.1f} % kept)")


def cmd_build(args) -> None:
    con = config.connect()
    stages = args.stages or STAGES
    t0 = time.time()
    if "mask" in stages:
        build_mask(con)
    if "coverage" in stages:
        build_coverage(con)
    if "cells" in stages:
        build_cells(con, args.grid)
    log(f"Done in {time.time() - t0:.0f}s")


# ----------------------------------------------------------------------------- check
def cmd_check(args) -> None:
    con = config.connect()
    agg = f"'{config.sql_path(config.agg_path(args.grid))}'"
    funnel = json.loads(funnel_path(args.grid).read_text(encoding="utf-8"))
    ok = True

    def report(good: bool, msg: str) -> None:
        nonlocal ok
        ok &= good
        log(f"   {'ok ' if good else 'BAD'} {msg}")

    log("1. Totals per satellite: cell table = rows kept by the funnel (independent query)")
    sums = con.execute("SELECT " + ", ".join(f"sum(n_{k})" for k in SATS) + f", sum(n_type_inferred) FROM {agg}").fetchone()
    for (key, sat), total in zip(SATS.items(), sums):
        kept = sum(r["kept"] for r in funnel if r["satellite"] == sat)
        report(total == kept, f"{sat:<6} cell table {total:,} = funnel {kept:,}")
    inferred = sum(r["kept_type_inferred"] for r in funnel)
    report(sums[-1] == inferred, f"rows with inferred type: cell table {sums[-1]:,} = funnel {inferred:,}")

    log("2. Internal consistency")
    bad_dn = con.execute(f"SELECT count(*) FROM {agg} WHERE " +
                         " OR ".join(f"n_{k}_d + n_{k}_n <> n_{k}" for k in SATS)).fetchone()[0]
    report(bad_dn == 0, f"day + night = total in every row ({bad_dn} violations)")
    bad_frp = con.execute(f"SELECT count(*) FROM {agg} WHERE " +
                          " OR ".join(f"frp_sum_{k} < 0 OR frp_sum_{k}_d + frp_sum_{k}_n > frp_sum_{k} + 1e-6 * frp_sum_{k} + 1e-6"
                                      for k in SATS)).fetchone()[0]
    report(bad_frp == 0, f"FRP sums >= 0 and day + night = total ({bad_frp} violations)")
    n, keys = con.execute(f"SELECT count(*), count(DISTINCT (cell_lat, cell_lon, year, month)) FROM {agg}").fetchone()
    report(n == keys, f"one row per cell-month ({n:,} rows, {keys:,} keys)")
    g = args.grid
    lo, hi = S.calendar_lat_range
    r = con.execute(f"""SELECT min(cell_lat), max(cell_lat), min(cell_lon), max(cell_lon),
            count(*) FILTER (WHERE abs(cell_lat / {g} - round(cell_lat / {g})) > 1e-6 OR abs(cell_lon / {g} - round(cell_lon / {g})) > 1e-6),
            count(*) FILTER (WHERE """ + " AND ".join(f"n_{k} = 0" for k in SATS) + f""") FROM {agg}""").fetchone()
    report(r[0] >= lo - g and r[1] <= hi and r[2] >= -180 and r[3] < 180 and r[4] == 0 and r[5] == 0,
           f"cells on the {g} deg grid, lat {r[0]} .. {r[1]}, lon {r[2]} .. {r[3]}, {r[4]} off-grid, {r[5]} empty rows")
    stats = con.execute(f"""SELECT row_group_id, stats_min_value, stats_max_value FROM parquet_metadata({agg})
                            WHERE path_in_schema = 'cell_lat' ORDER BY row_group_id""").fetchall()
    unsorted = sum(1 for a, b in zip(stats, stats[1:]) if float(b[1]) < float(a[2]))
    report(unsorted == 0, f"sorted by cell ({len(stats)} row groups, {unsorted} out of order)")

    log("3. Coverage table against the step-1 inventory (gaps.json)")
    gaps = json.loads((config.PROCESSED_DIR / "inventory" / "gaps.json").read_text(encoding="utf-8"))
    cov = {s: (m, lw) for s, m, lw in con.execute(
        f"SELECT satellite, sum(missing_days), sum(low_days) FROM '{config.sql_path(config.coverage_path())}' GROUP BY 1").fetchall()}
    for sat in SATS.values():
        g_ = gaps[INVENTORY_SAT[sat]]
        m, lw = cov[sat]
        report(m == len(g_["missing"]) and lw == len(g_["low"]),
               f"{sat:<6} missing days {m} (inventory {len(g_['missing'])}), low days {lw} (inventory {len(g_['low'])})")

    log("4. NRT rows: share inferred as static, against the true static share in the SP rows of 2026")
    true_share = dict(con.execute(f"""SELECT satellite, count(*) FILTER (WHERE type <> 0) / count(*)
        FROM {config.hotspots_sql()} WHERE processing = 'SP' AND year = 2026 GROUP BY 1""").fetchall())
    for r in funnel:
        if r["processing"] == "NRT":
            share = r["nrt_inferred_static"] / r["total"]
            log(f"   {r['satellite']:<6} NRT {r['total']:,} rows, {100 * share:.1f} % inferred static "
                f"(SP 2026: {100 * true_share.get(r['satellite'], 0):.1f} % static)")

    log("5. Exclusions: clean type-0 detections removed (all satellites)")
    for e in config.EXCLUSIONS:
        log(f"   {e.name:<20} {sum(r['excl_' + e.name] for r in funnel):>8,}")
    polar = sum(r["after_exclusions"] - r["kept"] for r in funnel)
    clipped = sum(r["kept_frp_clipped"] for r in funnel)
    log(f"   outside {S.calendar_lat_range} latitude: {polar:,}; kept rows with FRP clipped to {S.frp_clip_min}: {clipped:,}")
    log("CHECK PASSED" if ok else "CHECK FAILED - see the lines marked BAD")
    if not ok:
        raise SystemExit(1)


# ----------------------------------------------------------------------------- info
def cmd_info(args) -> None:
    con = config.connect()
    out = config.agg_path(args.grid)
    agg = f"'{config.sql_path(out)}'"
    cols = con.execute(f"DESCRIBE SELECT * FROM {agg}").fetchall()
    n, cells = con.execute(f"SELECT count(*), count(DISTINCT (cell_lat, cell_lon)) FROM {agg}").fetchone()
    log(f"{out.name}: {n:,} rows, {cells:,} cells, {len(cols)} columns, {out.stat().st_size / 1e6:.1f} MB")
    log("   " + ", ".join(f"{c[0]} {c[1]}" for c in cols[:6]) + ", ... (6 per satellite: n, n_d, n_n, frp_sum, frp_sum_d, frp_sum_n)"
        + ", n_type_inferred")
    log("Calendar-clean detections per year (millions) and raw S-NPP / Aqua ratios")
    log("   year   Terra   Aqua      N    N20    N21   N/Aqua day  N/Aqua night  rows with inferred type")
    for r in con.execute(f"""SELECT year, sum(n_terra), sum(n_aqua), sum(n_snpp), sum(n_n20), sum(n_n21),
            sum(n_snpp_d) / nullif(sum(n_aqua_d), 0), sum(n_snpp_n) / nullif(sum(n_aqua_n), 0), sum(n_type_inferred)
            FROM {agg} GROUP BY 1 ORDER BY 1""").fetchall():
        ratio = lambda v: f"{v:11.2f}" if v is not None else f"{'-':>11}"
        log(f"   {r[0]}  " + " ".join(f"{v / 1e6:6.2f}" for v in r[1:6]) + f"  {ratio(r[6])}  {ratio(r[7]):>12}  {r[8]:>10,}")
    log(f"Sample (5 rows with S-NPP and Aqua detections)")
    cur = con.execute(f"""SELECT cell_lat, cell_lon, year, month, n_terra, n_aqua, n_aqua_d, n_aqua_n, n_snpp, n_snpp_d, n_snpp_n,
            n_n20, n_n21, round(frp_sum_snpp, 1) AS frp_sum_snpp, n_type_inferred
            FROM {agg} WHERE n_aqua > 0 AND n_snpp > 0 AND hash(cell_lat, cell_lon, year, month) % 50021 = 0 LIMIT 5""")
    log("   " + " | ".join(d[0] for d in cur.description))
    for row in cur.fetchall():
        log("   " + " | ".join(str(v) for v in row))


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="static mask, coverage table and cell x month table")
    b.add_argument("--stages", nargs="+", choices=STAGES)
    b.set_defaults(func=cmd_build)
    c = sub.add_parser("check", help="consistency checks")
    c.set_defaults(func=cmd_check)
    i = sub.add_parser("info", help="schema, yearly totals and a sample")
    i.set_defaults(func=cmd_info)
    for sp in (b, c, i):
        sp.add_argument("--grid", type=float, default=S.grid_deg)
    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
