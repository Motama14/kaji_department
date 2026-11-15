# SI-assisted: written with Claude Code (Anthropic LLM) under the author's direction and reviewed by the author.
"""Step 3 - adapter: raw FIRMS CSVs (SP + NRT, four sensor folders) -> one detections table in Parquet.

Ported from the preparation project and adapted to the global data. One row per
detection, a single internal schema for every source:

    sensor (MODIS|VIIRS), satellite (Terra|Aqua|N|N20|N21), year      <- from the folder path (hive)
    lat, lon, datetime_utc, date, month, daynight (D|N),
    confidence_raw, confidence_norm (low|nominal|high), frp,
    brightness, bright_t31, scan, track, type (NULL in NRT), version,
    processing (SP|NRT), source_file

Layout:  <processed>/hotspots/sensor=<S>/satellite=<SAT>/year=<YYYY>/<source file stem>.parquet
Every Parquet file is sorted by time and written in small row groups, so a day query
reads only a few row groups.

How it works
- One source CSV at a time (the files reach 1.8 GB), in chronological order per folder.
- The sensor comes from the folder (FIRMS product code), never from the CSV; the
  `satellite` column is cross-checked against it.
- Nothing is coerced silently: every column is read as text, and the run stops if a
  line is rejected or a value does not parse.
- Deduplication: rows of a file that are exact copies of rows already written from
  another file of the same folder, inside the time window both files share, are
  dropped (the earlier file wins). Overlapping rows that are *not* identical are kept
  and reported. Repeated keys inside one file are never removed here, only reported
  by `check`.
- A manifest (`hotspots/_manifest.json`) records what each source produced, so an
  interrupted run resumes and `check` can audit the result.

Run from the repo root:
    .venv/Scripts/python -m pipeline.adapter convert --only "fire_archive_SV-C2_845668.csv"
    .venv/Scripts/python -m pipeline.adapter convert          # everything not converted yet
    .venv/Scripts/python -m pipeline.adapter check            # audit against the step-1 inventory
    .venv/Scripts/python -m pipeline.adapter info             # schema, sizes per column, sample
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import os
import time
from pathlib import Path

from . import config
from .inventory import check_header, csv_source, discover, read_edges

HOTSPOTS = config.hotspots_dir()
MANIFEST = HOTSPOTS / "_manifest.json"
INVENTORY_DIR = config.PROCESSED_DIR / "inventory"
HIVE_TYPES = "hive_types={'sensor': VARCHAR, 'satellite': VARCHAR, 'year': SMALLINT}"

# Columns stored inside each Parquet file, in order (sensor, satellite and year live in the path).
OUT_COLS = [
    "lat", "lon", "datetime_utc", "date", "month", "daynight", "confidence_raw", "confidence_norm",
    "frp", "brightness", "bright_t31", "scan", "track", "type", "version", "processing", "source_file",
]
# A row is "the same detection delivered twice" only when all of these match.
SAME_ROW_COLS = [
    "satellite", "lat", "lon", "datetime_utc", "daynight", "confidence_raw", "frp",
    "brightness", "bright_t31", "scan", "track", "type", "version",
]
NUMERIC_COLS = ["frp", "brightness", "bright_t31", "scan", "track"]
TIME_RE = "([01][0-9]|2[0-3])[0-5][0-9]"  # acq_time is HHMM, always 4 digits


def log(msg: str) -> None:
    print(msg, flush=True)


def ts_sql(iso: str) -> str:
    return f"TIMESTAMP '{iso.replace('T', ' ')}'"


# ----------------------------------------------------------------------------- manifest
def load_manifest() -> dict:
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {"layout": "sensor/satellite/year", "files": {}}


def save_manifest(manifest: dict) -> None:
    HOTSPOTS.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    os.replace(tmp, MANIFEST)


def is_done(f: dict, entry: dict | None) -> bool:
    if not entry or entry["size_bytes"] != f["size_bytes"] or entry["mtime"] != f["mtime"]:
        return False
    return all((HOTSPOTS / o["path"]).exists() for o in entry["outputs"])


def remove_outputs(f: dict, entry: dict | None) -> None:
    """Delete the Parquet files a source produced before (our own, regenerable outputs)."""
    stem = Path(f["file"]).stem
    stale = {HOTSPOTS / o["path"] for o in (entry or {}).get("outputs", [])}
    if HOTSPOTS.exists():
        stale |= set(HOTSPOTS.rglob(f"{stem}.parquet")) | set(HOTSPOTS.rglob(f"{stem}.parquet.tmp"))
    for p in stale:
        p.unlink(missing_ok=True)


# ----------------------------------------------------------------------------- mapping
def mapping_sql(f: dict, header: list[str]) -> str:
    """SELECT that maps one raw CSV (all columns as text) to typed internal columns."""
    src = config.SOURCES[f["folder"]]
    sat_case = " ".join(f"WHEN '{raw}' THEN '{name}'" for raw, name in src.satellites.items())
    if src.sensor == "MODIS":
        # FIRMS bands for MODIS: < 30 low, 30-79 nominal, >= 80 high. The low/nominal cut is the
        # configurable cleaning threshold, so filtering on confidence_norm works for both sensors.
        c = "TRY_CAST(confidence AS INTEGER)"
        conf_norm = (f"CASE WHEN {c} BETWEEN 80 AND 100 THEN 'high' "
                     f"WHEN {c} >= {config.SETTINGS.modis_min_confidence} AND {c} < 80 THEN 'nominal' "
                     f"WHEN {c} >= 0 AND {c} < 80 THEN 'low' END")
    else:
        conf_norm = "CASE confidence WHEN 'h' THEN 'high' WHEN 'n' THEN 'nominal' WHEN 'l' THEN 'low' END"
    type_expr = "TRY_CAST(type AS TINYINT)" if "type" in header else "CAST(NULL AS TINYINT)"
    numeric = ",\n        ".join(f"TRY_CAST({c} AS DOUBLE) AS {c}" for c in NUMERIC_COLS)
    return f"""
    SELECT
        CASE satellite {sat_case} END AS satellite,
        TRY_CAST(latitude AS DOUBLE) AS lat,
        TRY_CAST(longitude AS DOUBLE) AS lon,
        CASE WHEN regexp_full_match(acq_time, '{TIME_RE}') THEN
            TRY_CAST(acq_date AS DATE)
            + to_hours(CAST(substr(acq_time, 1, 2) AS INTEGER))
            + to_minutes(CAST(substr(acq_time, 3, 2) AS INTEGER)) END AS datetime_utc,
        daynight,
        confidence AS confidence_raw,
        {conf_norm} AS confidence_norm,
        {numeric},
        {type_expr} AS type,
        version
    FROM {csv_source(Path(f['path']), header)}"""


def validate(con, f: dict, has_type: bool) -> dict:
    """Count everything that would be a silent coercion. Any non-zero figure stops the run."""
    bad_num = " OR ".join(f"{c} IS NULL OR NOT isfinite({c})" for c in NUMERIC_COLS)
    row = con.execute(f"""
        SELECT count(*),
               count(*) FILTER (WHERE satellite IS NULL),
               count(*) FILTER (WHERE lat IS NULL OR lon IS NULL OR NOT isfinite(lat) OR NOT isfinite(lon)
                                      OR abs(lat) > 90 OR abs(lon) > 180),
               count(*) FILTER (WHERE datetime_utc IS NULL),
               count(*) FILTER (WHERE daynight IS NULL OR daynight NOT IN ('D', 'N')),
               count(*) FILTER (WHERE confidence_norm IS NULL),
               count(*) FILTER (WHERE {bad_num}),
               count(*) FILTER (WHERE type IS NULL),
               count(*) FILTER (WHERE version IS NULL),
               min(datetime_utc), max(datetime_utc)
        FROM raw""").fetchone()
    rejected = con.execute("SELECT count(DISTINCT line) FROM reject_errors").fetchone()[0]
    n = row[0]
    problems = {
        "rejected_lines": rejected, "bad_satellite": row[1], "bad_position": row[2], "bad_datetime": row[3],
        "bad_daynight": row[4], "bad_confidence": row[5], "bad_numeric": row[6],
        "bad_type": row[7] if has_type else 0, "bad_version": row[8],
    }
    if any(problems.values()) or n == 0:
        raise SystemExit(f"STOP: {f['folder']}/{f['file']} has values the adapter will not coerce: "
                         f"rows={n} {problems}. Nothing was written for this file.")
    return {"rows_in": n, "min_datetime": row[9].isoformat(), "max_datetime": row[10].isoformat()}


# ----------------------------------------------------------------------------- deduplication
def deduplicate(con, f: dict, stats: dict, manifest: dict) -> list[dict]:
    """Drop rows already written from another file of the same folder (exact copies only)."""
    report = []
    for other in manifest["files"].values():
        if other["folder"] != f["folder"] or other["file"] == f["file"] or not other["rows_written"]:
            continue
        lo = max(other["min_datetime"], stats["min_datetime"])
        hi = min(other["max_datetime"], stats["max_datetime"])
        if lo > hi:
            continue
        paths = ", ".join(f"'{config.sql_path(HOTSPOTS / o['path'])}'" for o in other["outputs"])
        window = f"datetime_utc BETWEEN {ts_sql(lo)} AND {ts_sql(hi)}"
        con.execute(f"""CREATE OR REPLACE TEMP TABLE prev AS
            SELECT DISTINCT {', '.join(SAME_ROW_COLS)}
            FROM read_parquet([{paths}], hive_partitioning=true, {HIVE_TYPES}) WHERE {window}""")
        in_window = con.execute(f"SELECT count(*) FROM raw WHERE {window}").fetchone()[0]
        same = " AND ".join(f"raw.{c} IS NOT DISTINCT FROM prev.{c}" for c in SAME_ROW_COLS)
        con.execute(f"DELETE FROM raw USING prev WHERE raw.{window} AND {same}")
        kept = con.execute(f"SELECT count(*) FROM raw WHERE {window}").fetchone()[0]
        report.append({"against": other["file"], "window": [lo, hi], "rows_in_window": in_window,
                       "dropped_identical": in_window - kept, "kept_not_identical": kept})
        con.execute("DROP TABLE prev")
    return report


# ----------------------------------------------------------------------------- convert
def convert_file(f: dict, manifest: dict) -> dict:
    src = config.SOURCES[f["folder"]]
    stem = Path(f["file"]).stem
    header, _, _ = read_edges(Path(f["path"]))
    check_header(f, header)
    has_type = "type" in header
    if has_type != (f["processing"] == "SP"):
        raise SystemExit(f"STOP: {f['file']}: `type` column {'present' if has_type else 'missing'} "
                         f"in a {f['processing']} file (expected the opposite).")
    t0 = time.time()
    con = config.connect()
    try:
        con.execute(f"CREATE TEMP TABLE raw AS {mapping_sql(f, header)}")
        t_load = time.time() - t0
        stats = validate(con, f, has_type)
        dedup = deduplicate(con, f, stats, manifest)
        dropped = sum(d["dropped_identical"] for d in dedup)

        s = config.SETTINGS
        parts = con.execute(
            "SELECT satellite, year(datetime_utc) AS y, count(*), min(datetime_utc), max(datetime_utc) "
            "FROM raw GROUP BY ALL ORDER BY ALL").fetchall()
        outputs, tmp_paths = [], []
        for sat, year, n, _, _ in parts:
            rel = f"sensor={src.sensor}/satellite={sat}/year={year}/{stem}.parquet"
            final = HOTSPOTS / rel
            tmp = final.with_name(final.name + ".tmp")
            final.parent.mkdir(parents=True, exist_ok=True)
            con.execute(f"""COPY (
                SELECT lat, lon, datetime_utc, CAST(datetime_utc AS DATE) AS date,
                       CAST(month(datetime_utc) AS TINYINT) AS month, daynight, confidence_raw, confidence_norm,
                       {', '.join(NUMERIC_COLS)}, type, version,
                       '{f['processing']}' AS processing, '{f['file']}' AS source_file
                FROM raw WHERE satellite = '{sat}' AND year(datetime_utc) = {year}
                ORDER BY datetime_utc, lat, lon
            ) TO '{config.sql_path(tmp)}'
              (FORMAT parquet, COMPRESSION {s.parquet_compression}, ROW_GROUP_SIZE {s.parquet_row_group_size})""")
            written = con.execute(f"SELECT count(*) FROM read_parquet('{config.sql_path(tmp)}')").fetchone()[0]
            if written != n:
                raise SystemExit(f"STOP: {rel}: wrote {written} rows, expected {n}.")
            tmp_paths.append((tmp, final))
            outputs.append({"path": rel, "satellite": sat, "year": int(year), "rows": n})
        if parts:
            stats["min_datetime"] = min(p[3] for p in parts).isoformat()
            stats["max_datetime"] = max(p[4] for p in parts).isoformat()
    finally:
        con.close()

    for (tmp, final), o in zip(tmp_paths, outputs):
        os.replace(tmp, final)
        o["bytes"] = final.stat().st_size
    rows_written = sum(o["rows"] for o in outputs)
    if rows_written + dropped != stats["rows_in"]:
        raise SystemExit(f"STOP: {f['file']}: {stats['rows_in']} rows in, {rows_written} written + {dropped} dropped.")
    return {
        "file": f["file"], "folder": f["folder"], "sensor": src.sensor, "processing": f["processing"],
        "size_bytes": f["size_bytes"], "mtime": f["mtime"],
        "rows_in": stats["rows_in"], "rows_dup_dropped": dropped, "rows_written": rows_written,
        "min_datetime": stats["min_datetime"], "max_datetime": stats["max_datetime"],
        "dedup": dedup, "outputs": outputs, "bytes_out": sum(o["bytes"] for o in outputs),
        "seconds_load": round(t_load, 1), "seconds": round(time.time() - t0, 1),
        "converted_at": dt.datetime.now().isoformat(timespec="seconds"),
    }


def cmd_convert(args) -> None:
    folders = list(config.SOURCES)
    files = sorted(discover(), key=lambda f: (folders.index(f["folder"]), f["processing"] == "NRT", int(f["file_id"])))
    if args.only:
        files = [f for f in files if any(fnmatch.fnmatch(f["file"], g) for g in args.only)]
    if not files:
        raise SystemExit("No files match.")
    manifest = load_manifest()
    t0 = time.time()
    done_bytes = 0
    for i, f in enumerate(files, 1):
        entry = manifest["files"].get(f["file"])
        if not args.force and is_done(f, entry):
            log(f"[{i}/{len(files)}] {f['folder']}/{f['file']}: already converted, skipped")
            continue
        remove_outputs(f, entry)
        manifest["files"].pop(f["file"], None)
        rec = convert_file(f, manifest)
        manifest["files"][f["file"]] = rec
        save_manifest(manifest)
        done_bytes += f["size_bytes"]
        dd = f", dropped {rec['rows_dup_dropped']:,} duplicates" if rec["dedup"] else ""
        log(f"[{i}/{len(files)}] {f['folder']}/{f['file']}: {rec['rows_in']:,} rows in -> "
            f"{rec['rows_written']:,} written{dd}, {len(rec['outputs'])} files, "
            f"{rec['bytes_out'] / 1e6:.1f} MB ({rec['bytes_out'] / max(rec['rows_written'], 1):.1f} B/row), "
            f"{rec['seconds']:.0f}s ({f['size_bytes'] / 1e6 / max(rec['seconds'], 0.1):.0f} MB/s)")
        for d in rec["dedup"]:
            flag = "" if not d["kept_not_identical"] else "  <-- CHECK: overlapping rows that are not identical were kept"
            log(f"      overlap with {d['against']} {d['window'][0]} .. {d['window'][1]}: "
                f"{d['dropped_identical']:,} identical rows dropped, {d['kept_not_identical']:,} kept{flag}")
    log(f"Done: {done_bytes / 1e9:.2f} GB of CSV in {time.time() - t0:.0f}s. Manifest: {MANIFEST}")


# ----------------------------------------------------------------------------- check
def cmd_check(args) -> None:
    manifest = load_manifest()
    files = manifest["files"]
    if not files:
        raise SystemExit("Nothing converted yet.")
    inv = {r["file"]: r for r in json.loads((INVENTORY_DIR / "files.json").read_text(encoding="utf-8"))}
    ok = True

    log("1. Row accounting (inventory rows = rows in = written + dropped)")
    for name, m in files.items():
        good = m["rows_in"] == inv[name]["n_rows"] and m["rows_in"] == m["rows_written"] + m["rows_dup_dropped"]
        ok &= good
        log(f"   {'ok ' if good else 'BAD'} {name}: inventory {inv[name]['n_rows']:,}, in {m['rows_in']:,}, "
            f"written {m['rows_written']:,}, dropped {m['rows_dup_dropped']:,}")

    con = config.connect()
    hot = config.hotspots_sql()
    names = ", ".join(f"'{n}'" for n in files)
    daily = config.sql_path(INVENTORY_DIR / "daily_counts.parquet")
    log("2. Per day x satellite x day/night x confidence class x type, against the step-1 inventory")
    con.execute(f"""CREATE TEMP TABLE cmp AS
        WITH o AS (SELECT source_file AS file, date, satellite, daynight, confidence_norm AS conf_class, type,
                          count(*) AS n, sum(frp) AS frp_sum
                   FROM {hot} GROUP BY ALL),
             i AS (SELECT file, date, CASE satellite WHEN 'SNPP' THEN 'N' ELSE satellite END AS satellite,
                          daynight, conf_class, type, sum(n) AS n, sum(frp_sum) AS frp_sum
                   FROM '{daily}' WHERE file IN ({names}) GROUP BY ALL)
        SELECT coalesce(o.file, i.file) AS file, coalesce(o.date, i.date) AS date,
               coalesce(i.n, 0) AS inv_n, coalesce(o.n, 0) AS out_n,
               coalesce(i.frp_sum, 0) AS inv_frp, coalesce(o.frp_sum, 0) AS out_frp
        FROM o FULL JOIN i ON o.file = i.file AND o.date = i.date AND o.satellite = i.satellite
             AND o.daynight = i.daynight AND o.conf_class = i.conf_class AND o.type IS NOT DISTINCT FROM i.type""")
    groups, same_n, same_frp = con.execute(
        "SELECT count(*), count(*) FILTER (WHERE inv_n = out_n), "
        "count(*) FILTER (WHERE abs(inv_frp - out_frp) <= 1e-6 * greatest(abs(inv_frp), 1)) FROM cmp").fetchone()
    log(f"   {groups:,} groups compared: {same_n:,} with identical counts, {same_frp:,} with identical FRP sums")
    diffs = con.execute("SELECT file, date, sum(inv_n), sum(out_n) FROM cmp WHERE inv_n <> out_n "
                        "GROUP BY ALL ORDER BY ALL").fetchall()
    by_file: dict[str, int] = {}
    for file, date, inv_n, out_n in diffs:
        windows = [d["window"] for d in files[file]["dedup"]]
        explained = any(w[0][:10] <= date.isoformat() <= w[1][:10] for w in windows)
        by_file[file] = by_file.get(file, 0) + (inv_n - out_n)
        ok &= explained
        log(f"   {file} {date}: inventory {inv_n:,}, output {out_n:,} "
            f"({'inside a deduplication window' if explained else 'NOT EXPLAINED'})")
    for file, missing in by_file.items():
        good = missing == files[file]["rows_dup_dropped"]
        ok &= good
        log(f"   {'ok ' if good else 'BAD'} {file}: {missing:,} rows fewer than the inventory, "
            f"{files[file]['rows_dup_dropped']:,} dropped as duplicates")

    log("3. Repeated keys (same satellite, lat, lon, minute) per partition - reported, not removed")
    parts = sorted({(o["satellite"], o["year"]) for m in files.values() for o in m["outputs"]})
    attrs = "daynight, confidence_raw, frp, brightness, bright_t31, scan, track, type, version"
    for sat, year in parts:
        n, keys, extra, identical, cross = con.execute(f"""
            SELECT (SELECT count(*) FROM {hot} WHERE satellite = '{sat}' AND year = {year}),
                   count(*), coalesce(sum(c - 1), 0), coalesce(sum(c - d), 0), count(*) FILTER (WHERE nf > 1)
            FROM (SELECT count(*) AS c, count(DISTINCT ({attrs})) AS d, count(DISTINCT source_file) AS nf
                  FROM {hot} WHERE satellite = '{sat}' AND year = {year}
                  GROUP BY lat, lon, datetime_utc HAVING count(*) > 1)""").fetchone()
        ok &= cross == 0
        log(f"   {sat} {year}: {n:,} rows, {keys:,} repeated keys ({extra:,} extra rows, "
            f"{identical:,} of them exact copies, {cross:,} keys spanning two source files)")

    log("4. Time order inside each file (row-group statistics)")
    rows = con.execute(f"""SELECT file_name, row_group_id, stats_min_value, stats_max_value
        FROM parquet_metadata('{config.sql_path(HOTSPOTS)}/**/*.parquet')
        WHERE path_in_schema = 'datetime_utc' ORDER BY file_name, row_group_id""").fetchall()
    unsorted, prev = set(), (None, None)
    for name, _, lo, hi in rows:
        if prev[0] == name and lo < prev[1]:
            unsorted.add(name)
        prev = (name, hi)
    n_files = len({r[0] for r in rows})
    ok &= not unsorted
    log(f"   {n_files} files, {len(rows):,} row groups, {len(unsorted)} files out of order {sorted(unsorted) or ''}")
    log("CHECK PASSED" if ok else "CHECK FAILED - see the lines marked BAD / NOT EXPLAINED above")
    if not ok:
        raise SystemExit(1)


# ----------------------------------------------------------------------------- info
def cmd_info(args) -> None:
    manifest = load_manifest()
    con = config.connect()
    hot = config.hotspots_sql()
    glob = f"'{config.sql_path(HOTSPOTS)}/**/*.parquet'"
    log("Schema")
    for name, typ, *_ in con.execute(f"DESCRIBE SELECT * FROM {hot}").fetchall():
        log(f"   {name:<16} {typ}")
    rows = sum(m["rows_written"] for m in manifest["files"].values())
    size = sum(m["bytes_out"] for m in manifest["files"].values())
    csv = sum(m["size_bytes"] for m in manifest["files"].values())
    log(f"Totals: {len(manifest['files'])} source files, {rows:,} rows, {size / 1e6:.1f} MB Parquet "
        f"({size / max(rows, 1):.2f} bytes/row, {100 * size / max(csv, 1):.1f} % of the CSV size)")
    log("Compressed size per column")
    for col, c, u in con.execute(f"""SELECT path_in_schema, sum(total_compressed_size), sum(total_uncompressed_size)
            FROM parquet_metadata({glob}) GROUP BY 1 ORDER BY 2 DESC""").fetchall():
        log(f"   {col:<16} {c / 1e6:9.1f} MB  {c / max(rows, 1):6.2f} B/row  ({100 * c / size:4.1f} %)  raw {u / 1e6:9.1f} MB")
    log("Rows per satellite and year")
    for sat, year, n, nf in con.execute(
            f"SELECT satellite, year, count(*), count(DISTINCT source_file) FROM {hot} GROUP BY ALL ORDER BY ALL").fetchall():
        log(f"   {sat:<6} {year}  {n:>12,}  ({nf} files)")
    # One pseudo-random row from each of a few output files spread over the table (cheap: stops at the first hit).
    outs = [o["path"] for m in manifest["files"].values() for o in m["outputs"]]
    picked = outs[::max(1, len(outs) // max(args.sample, 1))][:args.sample]
    log(f"Sample ({len(picked)} rows, one per output file)")
    for k, rel in enumerate(picked):
        cur = con.execute(f"""SELECT * FROM read_parquet('{config.sql_path(HOTSPOTS / rel)}', hive_partitioning=true, {HIVE_TYPES})
            WHERE hash(lat, lon, datetime_utc) % 9973 = 0 LIMIT 1""")
        if k == 0:
            log("   " + " | ".join(d[0] for d in cur.description))
        for r in cur.fetchall():
            log("   " + " | ".join(str(v) for v in r))


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("convert", help="raw CSV -> Parquet (resumable)")
    c.add_argument("--only", nargs="+", metavar="GLOB", help="file-name patterns, e.g. \"fire_nrt_*\"")
    c.add_argument("--force", action="store_true", help="convert again even if already done")
    c.set_defaults(func=cmd_convert)
    k = sub.add_parser("check", help="audit the Parquet output against the step-1 inventory")
    k.set_defaults(func=cmd_check)
    i = sub.add_parser("info", help="schema, sizes per column and a sample")
    i.add_argument("--sample", type=int, default=8)
    i.set_defaults(func=cmd_info)
    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
