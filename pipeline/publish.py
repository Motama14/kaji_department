# SI-assisted: written with Claude Code (Anthropic LLM) under the author's direction and reviewed by the author.
"""Step 8 - what the API reads besides the harmonized table: enriched detections and the daily counts table.

Ported from the preparation project's publish.py and adapted (user decision in step 2): there is no second
copy of the points. The API reads the single detections table (data/processed/hotspots/) and adds these
columns at query time with `points_sql()`:

  type           effective type: the FIRMS type for SP rows, the inferred type for NRT rows (step 5 rule)
  type_firms     the FIRMS type as delivered (NULL for NRT rows)
  type_inferred  TRUE for NRT rows: their type was calculated from the SP history, FIRMS did not deliver one
  in_calendar    the detection is counted in the burn calendar (same rule as pipeline.aggregate.keep_sql)
  series_sat     the detection's satellite feeds the harmonized series in that month
  weight         VIIRS-equivalent detections this point stands for in the harmonized series:
                 n_harmonized / (detections of the series satellite) in its cell-month; 0 when it is not
                 in the calendar or its satellite does not feed the series that month
  harmonized_source  the `source` code of its cell-month in the harmonized table

The weights of a cell-month add up to its harmonized value only when the series satellite saw at least one
detection there; cell-months where the model predicts fires that no MODIS detection carries (about 6 % of
the MODIS-era total, docs/RESULTS.md) have no point to carry them.

Output written here (one pass over the detections, year by year):
  daily_counts.parquet   per date x satellite x effective type x confidence x day/night x calendar flag x
                         series flag x inferred flag: count `n` and harmonized sum `w` (points outside the
                         calendar count 1). Feeds the viewer's timeline histogram and /api/available-dates.
  serving_facts.json     what the API would otherwise compute at startup by scanning the detections and the
                         harmonized table (latest detection date, series satellite per month, first NRT date,
                         months with inferred types), plus a fingerprint (bytes + row count) of the files they
                         come from. The API reads it at startup; if it is missing or its fingerprint does not match,
                         the API logs a warning and computes the facts live (Cloud Run cold start, step 10).
                         It also lists every detections file with its first / last date and rows, so queries
                         open only the files for their dates instead of walking the folders (step 10c). After any
                         change to hotspots/, rewrite it (`facts`); `info` compares the list with the folder.

  area_month_g0.25.parquet  the same breakdown per 0.25 deg cell x month (instead of per date): the viewer's monthly
                         area timeline reads it instead of scanning years of detections (step 10e).

Run from the repo root:
    .venv/Scripts/python -m pipeline.publish build     # daily counts + facts, about 6-10 min
    .venv/Scripts/python -m pipeline.publish area      # area timeline table, about 10-15 min
    .venv/Scripts/python -m pipeline.publish facts     # facts file only, a few seconds
    .venv/Scripts/python -m pipeline.publish info
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import json
import logging
import shutil
import time
from pathlib import Path

from . import config
from .aggregate import keep_sql, load_mask, mask_key

S = config.SETTINGS
AQUA_START = dt.date(2002, 7, 1)
# Source code of the harmonized table -> satellite whose detections carry it (pipeline/calibrate.py docstring).
SOURCE_SAT = {"VIIRS_SNPP": "N", "VIIRS_N20_SCALED": "N20", "VIIRS_N21_SCALED": "N21",
              "MODIS_AQUA_MODEL": "Aqua", "MODIS_TERRA_MODEL": "Terra"}
SAT_COUNT_COL = {"Terra": "n_terra", "Aqua": "n_aqua", "N": "n_snpp", "N20": "n_n20", "N21": "n_n21"}


def daily_path():
    return config.PROCESSED_DIR / "daily_counts.parquet"


def facts_path():
    return config.PROCESSED_DIR / "serving_facts.json"


def area_path():
    return config.PROCESSED_DIR / f"area_month_g{S.grid_deg:g}.parquet"


FACTS_VERSION = 2  # 2: + hotspot_files (step 10c)
_log = logging.getLogger("pipeline.publish")


def log(msg: str) -> None:
    print(msg, flush=True)


# ----------------------------------------------------------------------------- series month table
def series_rows(con, last: dt.date) -> list[tuple]:
    """(year, month, source, series_sat, days_fraction, ym) per month, computed from the harmonized table.

    One source per month (the S-NPP outage logic is global per month). VIIRS_EXPOSURE months take the VIIRS
    satellite with the most observed days. days_fraction < 1 only for the last data month, whose harmonized
    value covers the days up to the last detection date (calibrate.prepare: expected_days = last day).
    """
    h = config.sql_path(config.harmonized_path())
    cov = config.sql_path(config.coverage_path())
    rows = con.execute(f"""SELECT year, month, list(DISTINCT source) FROM '{h}' GROUP BY ALL ORDER BY ALL""").fetchall()
    best_viirs = dict(((y, m), s) for y, m, s in con.execute(f"""
        SELECT year, month, arg_max(satellite, obs_days) FROM '{cov}' WHERE satellite IN ('N', 'N20', 'N21')
        GROUP BY ALL""").fetchall())
    out = []
    for y, m, sources in rows:
        if len(sources) != 1:
            raise SystemExit(f"{y}-{m:02d} has {len(sources)} harmonized sources ({sources}); expected one per month")
        src = sources[0]
        sat = SOURCE_SAT.get(src) or best_viirs[(y, m)]
        frac = last.day / calendar.monthrange(y, m)[1] if (y, m) == (last.year, last.month) else 1.0
        out.append((int(y), int(m), src, sat, round(frac, 4), int(y) * 100 + int(m)))
    return out


def build_series_month(con, facts: dict | None = None) -> list[tuple]:
    """Temp table series_month(year, month, source, series_sat, days_fraction, ym), from the facts file when
    given, otherwise computed live (which scans the detections for the latest date)."""
    if facts:
        out = [tuple(r) for r in facts["series_month"]]
    else:
        out = series_rows(con, last_date(con))
    con.execute("""CREATE OR REPLACE TEMP TABLE series_month (year SMALLINT, month TINYINT, source VARCHAR,
                   series_sat VARCHAR, days_fraction DOUBLE, ym INTEGER)""")
    con.executemany("INSERT INTO series_month VALUES (?, ?, ?, ?, ?, ?)", out)
    global _SERIES_RUNS
    _SERIES_RUNS = []  # (first ym, last ym, satellite) runs of consecutive months with the same series satellite
    for *_, sat, _, ym in out:
        if _SERIES_RUNS and _SERIES_RUNS[-1][2] == sat:
            _SERIES_RUNS[-1][1] = ym
        else:
            _SERIES_RUNS.append([ym, ym, sat])
    return out


_SERIES_RUNS: list[list] = []


def series_sat_sql(year: str, month: str) -> str:
    """SQL CASE giving the series satellite of a month (from build_series_month), without a join.

    A join with series_month followed by a filter on it made DuckDB pick a slow plan (20 s instead of 3 s for a
    global month of detections), so the 313-month table is inlined as a few ranges.
    """
    if not _SERIES_RUNS:
        raise RuntimeError("call prepare() / build_series_month() first")
    ym = f"(CAST({year} AS INTEGER) * 100 + {month})"
    whens = " ".join(f"WHEN {ym} BETWEEN {a} AND {b} THEN '{s}'" for a, b, s in _SERIES_RUNS)
    return f"CASE {whens} END"


def last_date(con) -> dt.date:
    hot = config.hotspots_sql()
    return con.execute(f"SELECT max(date) FROM {hot} WHERE year = (SELECT max(year) FROM {hot})").fetchone()[0]


# ----------------------------------------------------------------------------- serving facts (cold start)
FACTS: dict | None = None  # facts used by the last prepare(): from the file, or None when computed live


def _fingerprint() -> dict:
    """Bytes and row count of the files the facts are derived from. Modification times are not used: they do
    not survive the source upload and the image build, sizes and row counts do."""
    import pyarrow.parquet as pq

    out = {}
    for p in (config.harmonized_path(), config.coverage_path(), daily_path()):
        out[p.name] = {"bytes": p.stat().st_size, "rows": pq.read_metadata(p).num_rows} if p.exists() else None
    return out


def compute_facts(con) -> dict:
    """The startup facts, computed live (scans the detections and the harmonized table; needs the daily table)."""
    last = last_date(con)
    rows = series_rows(con, last)
    d = config.sql_path(daily_path())
    nrt_first = con.execute(f"SELECT min(date) FILTER (WHERE type_inferred) FROM '{d}'").fetchone()[0]
    h = config.sql_path(config.harmonized_path())
    inferred = [f"{y}-{m:02d}" for y, m in con.execute(
        f"SELECT DISTINCT year, month FROM '{h}' WHERE type_inferred ORDER BY 1, 2").fetchall()]
    return {"last_date": last.isoformat(), "nrt_first": nrt_first.isoformat() if nrt_first else None,
            "type_inferred_months": inferred, "series_month": [list(r) for r in rows],
            "hotspot_files": hotspot_manifest(con)}


def hotspot_manifest(con) -> list[list]:
    """[path relative to the detections folder, first date, last date, rows] of every detections file, read from
    the Parquet footers (rows are date-sorted, so the `date` statistics are exact)."""
    root = config.hotspots_dir()
    rows = con.execute(f"""
        SELECT file_name, min(stats_min_value), max(stats_max_value), sum(row_group_num_rows)
        FROM parquet_metadata('{config.sql_path(root)}/**/*.parquet') WHERE path_in_schema = 'date'
        GROUP BY 1 ORDER BY 1""").fetchall()
    return [[Path(f).relative_to(root).as_posix(), mn, mx, int(n)] for f, mn, mx, n in rows]


def write_facts(con) -> dict:
    facts = {"version": FACTS_VERSION, "built_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "grid_deg": S.grid_deg, "inputs": _fingerprint(), **compute_facts(con)}
    out = facts_path()
    tmp = out.with_name(out.name + ".tmp")
    lists = ("series_month", "hotspot_files")  # one entry per line
    text = json.dumps({k: v for k, v in facts.items() if k not in lists}, indent=1)[:-2]
    for k in lists:
        text += f',\n "{k}": [\n' + ",\n".join("  " + json.dumps(r) for r in facts[k]) + "\n ]"
    tmp.write_text(text + "\n}\n", encoding="utf-8")
    tmp.replace(out)
    log(f"serving facts: last date {facts['last_date']}, NRT from {facts['nrt_first']}, "
        f"{len(facts['series_month'])} months, {len(facts['type_inferred_months'])} with inferred types, "
        f"{len(facts['hotspot_files'])} detection files -> {out.name} ({out.stat().st_size / 1e3:.1f} kB)")
    return facts


def load_facts(warn: bool = True) -> dict | None:
    """The facts file if it is present and matches its inputs; None (and a warning) otherwise."""
    p = facts_path()
    reason = None
    try:
        facts = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        if facts is None:
            reason = "missing"
        elif facts.get("version") != FACTS_VERSION or facts.get("grid_deg") != S.grid_deg:
            reason = "built by another version or grid"
        elif facts.get("inputs") != _fingerprint():
            reason = "older than its inputs (harmonized, coverage or daily counts changed)"
    except (OSError, ValueError) as e:
        facts, reason = None, f"unreadable ({e})"
    if reason:
        if warn:
            _log.warning("%s is %s: computing the startup facts live (scans the detections); "
                         "run `python -m pipeline.publish facts`", p.name, reason)
        return None
    return facts


def facts_status() -> str:
    """'file' when the API can start from the facts file, 'live' otherwise (for /api/health)."""
    return "file" if load_facts(warn=False) else "live"


def prepare(con, live: bool = False) -> None:
    """Temp tables used by points_sql(): static_mask and series_month. Uses the facts file unless `live`
    (the builds always recompute) or the file is missing / stale."""
    global FACTS
    load_mask(con)
    FACTS = None if live else load_facts()
    build_series_month(con, FACTS)


def hotspot_files(d0: dt.date | None = None, d1: dt.date | None = None) -> list[Path] | None:
    """Detections files whose dates overlap [d0, d1] (open ends allowed), from the facts file, so a query opens
    4-8 files for a day instead of walking every folder of the bucket mount (step 10c).

    None means "read the whole folder": no facts file loaded, or a listed file is missing (warning logged). A
    file added to hotspots/ after the facts were written is not seen: rewrite them (`facts`; `info` checks).
    """
    listed = (FACTS or {}).get("hotspot_files")
    if not listed:
        return None
    a, b = (d0.isoformat() if d0 else "0000-00-00"), (d1.isoformat() if d1 else "9999-99-99")
    root = config.hotspots_dir()
    rels = [rel for rel, first, last, _ in listed if first <= b and last >= a]
    if not rels:  # dates outside the data: one small file keeps the schema, the date filter returns no rows
        rels = [min(listed, key=lambda r: r[3])[0]]
    files = [root / rel for rel in rels]
    missing = [p for p in files if not p.exists()]
    if missing:
        _log.warning("%d detections file(s) listed in %s are missing (first: %s): reading the whole folder; "
                     "run `python -m pipeline.publish facts`", len(missing), facts_path().name, missing[0])
        return None
    return files


# ----------------------------------------------------------------------------- enriched points
def points_sql(where: str = "TRUE", harmonized_where: str = "TRUE", files: list[Path] | None = None) -> str:
    """Detections with effective type, calendar flag, series flag and weight (needs prepare()).

    `where` filters the detections table (columns of hotspots_sql(), alias h): put the date / year / bbox
    filters here so DuckDB prunes partitions and row groups. `harmonized_where` filters the harmonized table
    (alias z) to the same months, so the join does not read all 35 M cell-months. `files` (from hotspot_files())
    limits the read to those detections files; the `where` date filter must still be given.
    """
    g = S.grid_deg
    hz = config.sql_path(config.harmonized_path())
    conf = ", ".join(f"'{c}'" for c in S.viirs_min_confidence)
    n_series = "CASE e.satellite " + " ".join(f"WHEN '{s}' THEN z.{c}" for s, c in SAT_COUNT_COL.items()) + " END"
    return f"""
    WITH e AS (
      SELECT h.sensor, h.satellite, h.lat, h.lon, h.datetime_utc, h.date, h.year, h.month, h.daynight,
             h.confidence_raw, h.confidence_norm, h.frp, h.brightness, h.bright_t31, h.scan, h.track, h.version,
             h.processing, h.source_file,
             h.type AS type_firms,
             CAST(CASE WHEN h.processing = 'NRT' THEN coalesce(m.type, 0) ELSE h.type END AS TINYINT) AS type_eff,
             h.processing = 'NRT' AS type_inferred,
             CASE WHEN h.sensor = 'MODIS' THEN TRY_CAST(h.confidence_raw AS INTEGER) >= {S.modis_min_confidence}
                  ELSE h.confidence_norm IN ({conf}) END AS conf_ok
      FROM {config.hotspots_sql(files)} h
      LEFT JOIN static_mask m
        ON h.processing = 'NRT' AND m.gy = {mask_key('h.lat')} AND m.gx = {mask_key('h.lon')}
      WHERE {where}
    ), f AS (
      SELECT e.*, coalesce({keep_sql()}, false) AS in_calendar,
             e.satellite = {series_sat_sql('e.year', 'e.month')} AS is_series
      FROM e
    ), z AS (
      SELECT * FROM '{hz}' z WHERE {harmonized_where}
    )
    SELECT e.sensor, e.satellite, e.lat, e.lon, e.datetime_utc, e.date, e.year, e.month, e.daynight,
           e.confidence_raw, e.confidence_norm, e.frp, e.brightness, e.bright_t31, e.scan, e.track, e.version,
           e.processing, e.source_file, e.type_firms, e.type_eff AS type, e.type_inferred, e.in_calendar,
           coalesce(e.is_series, false) AS series_sat,
           CAST(CASE WHEN e.in_calendar AND e.is_series AND {n_series} > 0
                     THEN z.n_harmonized / {n_series} ELSE 0 END AS FLOAT) AS weight,
           z.source AS harmonized_source
    FROM f e
    LEFT JOIN z
      ON e.in_calendar AND z.year = e.year AND z.month = e.month
     AND z.cell_lat = round(floor(e.lat / {g}) * {g}, 6) AND z.cell_lon = round(floor(e.lon / {g}) * {g}, 6)
    """


# ----------------------------------------------------------------------------- daily counts
def build_daily(con) -> None:
    t0 = time.time()
    prepare(con, live=True)
    out = daily_path()
    parts = config.TMP_DIR / "daily_counts_parts"
    shutil.rmtree(parts, ignore_errors=True)
    parts.mkdir(parents=True)
    years = [r[0] for r in con.execute(f"SELECT DISTINCT year FROM {config.hotspots_sql()} ORDER BY 1").fetchall()]
    for y in years:
        t1 = time.time()
        con.execute(f"""COPY (
            SELECT date, satellite, series_sat, type, confidence_norm, daynight, in_calendar, type_inferred,
                   CAST(count(*) AS INTEGER) AS n,
                   CAST(sum(CASE WHEN weight > 0 THEN weight ELSE 1 END) AS DOUBLE) AS w
            FROM ({points_sql(f'h.year = {y}', f'z.year = {y}')})
            GROUP BY ALL
        ) TO '{config.sql_path(parts / f'{y}.parquet')}' (FORMAT parquet)""")
        log(f"   {y}: {time.time() - t1:.0f}s")
    tmp = out.with_name(out.name + ".tmp")
    con.execute(f"""COPY (SELECT * FROM read_parquet('{config.sql_path(parts)}/*.parquet') ORDER BY date, satellite)
                    TO '{config.sql_path(tmp)}' (FORMAT parquet, COMPRESSION zstd)""")
    tmp.replace(out)
    shutil.rmtree(parts, ignore_errors=True)
    n = con.execute(f"SELECT count(*) FROM '{config.sql_path(out)}'").fetchone()[0]
    log(f"daily counts: {n:,} rows, {out.stat().st_size / 1e6:.1f} MB -> {out.name} ({time.time() - t0:.0f}s)")
    check_daily(con)
    write_facts(con)


def check_daily(con) -> None:
    """The daily table must reproduce the detections count and the calendar counts of the cell table."""
    d = config.sql_path(daily_path())
    agg = config.sql_path(config.agg_path())
    hz = config.sql_path(config.harmonized_path())
    n_daily, n_cal, w_series = con.execute(
        f"SELECT sum(n), sum(n) FILTER (WHERE in_calendar), sum(w) FILTER (WHERE in_calendar AND series_sat) FROM '{d}'"
    ).fetchone()
    n_hot = con.execute(f"SELECT count(*) FROM {config.hotspots_sql()}").fetchone()[0]
    n_agg = con.execute(f"SELECT sum(n_terra + n_aqua + n_snpp + n_n20 + n_n21) FROM '{agg}'").fetchone()[0]
    h_carried = con.execute(f"""SELECT sum(z.n_harmonized) FROM '{hz}' z JOIN series_month s USING (year, month)
        WHERE CASE s.series_sat """ + " ".join(f"WHEN '{s}' THEN z.{c}" for s, c in SAT_COUNT_COL.items())
        + " END > 0").fetchone()[0]
    h_total = con.execute(f"SELECT sum(n_harmonized) FROM '{hz}'").fetchone()[0]
    ok = n_daily == n_hot and n_cal == n_agg and abs(w_series - h_carried) / h_carried < 1e-4
    log(f"check: detections {n_daily:,} (table {n_hot:,}); calendar {n_cal:,} (cell table {n_agg:,}); "
        f"weights of series points {w_series:,.0f} = harmonized carried by points {h_carried:,.0f} "
        f"({100 * h_carried / h_total:.1f} % of the harmonized total {h_total:,.0f}) -> {'OK' if ok else 'FAILED'}")
    if not ok:
        raise SystemExit("daily counts check failed")


# ----------------------------------------------------------------------------- area timeline table (step 10e)
def build_area(con) -> None:
    """area_month_g<grid>.parquet: the daily counts' breakdown per grid cell and month instead of per date, so the
    viewer's monthly area timeline needs no scan of the detections (user decision, step 10e)."""
    import duckdb

    t0 = time.time()
    prepare(con, live=True)
    g = S.grid_deg
    out = area_path()
    parts = config.TMP_DIR / "area_month_parts"
    shutil.rmtree(parts, ignore_errors=True)
    parts.mkdir(parents=True)
    sel = f"""
        SELECT CAST(round(floor(lat / {g}) * {g}, 6) AS FLOAT) AS cell_lat,
               CAST(round(floor(lon / {g}) * {g}, 6) AS FLOAT) AS cell_lon,
               make_date(year, month, 1) AS month_start, satellite, series_sat, type, confidence_norm, daynight,
               type_inferred, CAST(count(*) AS INTEGER) AS n,
               CAST(sum(CASE WHEN weight > 0 THEN weight ELSE 1 END) AS FLOAT) AS w
        FROM ({{pts}}) GROUP BY ALL"""
    years = [r[0] for r in con.execute(f"SELECT DISTINCT year FROM {config.hotspots_sql()} ORDER BY 1").fetchall()]
    for y in years:
        t1 = time.time()
        try:  # one pass per year; per month if the year does not fit in memory
            chunks = [(f"h.year = {y}", f"z.year = {y}", f"{y}")]
            con.execute(f"COPY ({sel.format(pts=points_sql(*chunks[0][:2]))}) "
                        f"TO '{config.sql_path(parts / f'{y}.parquet')}' (FORMAT parquet)")
        except duckdb.OutOfMemoryException:
            for m in range(1, 13):
                pts = points_sql(f"h.year = {y} AND h.month = {m}", f"z.year = {y} AND z.month = {m}")
                con.execute(f"COPY ({sel.format(pts=pts)}) "
                            f"TO '{config.sql_path(parts / f'{y}_{m:02d}.parquet')}' (FORMAT parquet)")
        log(f"   {y}: {time.time() - t1:.0f}s")
    tmp = out.with_name(out.name + ".tmp")
    # Sorted by cell first, so an area query reads only the row groups of its latitudes.
    con.execute(f"""COPY (SELECT * FROM read_parquet('{config.sql_path(parts)}/*.parquet')
                          ORDER BY cell_lat, cell_lon, month_start)
                    TO '{config.sql_path(tmp)}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 100000)""")
    tmp.replace(out)
    shutil.rmtree(parts, ignore_errors=True)
    n = con.execute(f"SELECT count(*) FROM '{config.sql_path(out)}'").fetchone()[0]
    log(f"area table: {n:,} rows, {out.stat().st_size / 1e6:.1f} MB -> {out.name} ({time.time() - t0:.0f}s)")
    check_area(con)


def check_area(con) -> None:
    """Per month, the area table must hold the same counts and harmonized sums as the daily counts table."""
    a, d = config.sql_path(area_path()), config.sql_path(daily_path())
    bad = con.execute(f"""
        WITH x AS (SELECT month_start AS m, sum(n) AS n, sum(w) AS w FROM '{a}' GROUP BY 1),
             y AS (SELECT date_trunc('month', date) AS m, sum(n) AS n, sum(w) AS w FROM '{d}' GROUP BY 1)
        SELECT count(*) FILTER (WHERE x.n IS DISTINCT FROM y.n OR abs(x.w - y.w) > 1e-4 * y.w + 1),
               sum(x.n), sum(y.n)
        FROM x FULL OUTER JOIN y ON x.m = y.m""").fetchone()
    log(f"check: area table {bad[1]:,} detections vs daily counts {bad[2]:,}; months that differ: {bad[0]} -> "
        f"{'OK' if bad[0] == 0 else 'FAILED'}")
    if bad[0]:
        raise SystemExit("area table check failed")


def info(con) -> None:
    d = config.sql_path(daily_path())
    for r in con.execute(f"""SELECT satellite, min(date), max(date), sum(n), sum(n) FILTER (WHERE in_calendar),
                             sum(n) FILTER (WHERE type_inferred) FROM '{d}' GROUP BY 1 ORDER BY 2""").fetchall():
        log(f"   {r[0]:<6} {r[1]} -> {r[2]}  {r[3]:>12,} detections, {r[4]:>12,} in calendar, {r[5]:>10,} type inferred")
    f = load_facts()
    log(f"   {facts_path().name}: {'OK, last date ' + f['last_date'] if f else 'missing or stale'}")
    a = area_path()
    if a.exists():
        n = con.execute(f"SELECT count(*) FROM '{config.sql_path(a)}'").fetchone()[0]
        log(f"   {a.name}: {n:,} rows, {a.stat().st_size / 1e6:.1f} MB")
    else:
        log(f"   {a.name}: missing (run `python -m pipeline.publish area`)")
    root = config.hotspots_dir()
    if f and root.exists():
        listed = {r[0] for r in f["hotspot_files"]}
        found = {p.relative_to(root).as_posix() for p in root.rglob("*.parquet")}
        log(f"   detections files: {len(listed)} listed, {len(found)} in {root.name}/ -> "
            f"{'OK' if listed == found else 'MISMATCH: run `python -m pipeline.publish facts`'}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build", help="daily counts table + serving facts (about 6-10 min)")
    sub.add_parser("facts", help="serving facts file only (a few seconds; needs the daily counts table)")
    sub.add_parser("area", help="area timeline table, cell x month (about 10-15 min; needs the daily counts table)")
    sub.add_parser("info", help="summary of the daily counts table and the facts file")
    args = ap.parse_args(argv)
    con = config.connect()
    if args.cmd == "build":
        build_daily(con)
    elif args.cmd == "facts":
        write_facts(con)
    elif args.cmd == "area":
        build_area(con)
    else:
        info(con)


if __name__ == "__main__":
    main()
