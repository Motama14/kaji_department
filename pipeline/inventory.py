# SI-assisted: written with Claude Code (Anthropic LLM) under the author's direction and reviewed by the author.
"""Step 1 - streamed inventory of the raw FIRMS CSVs (read-only on ``data/``).

Every file is scanned once with DuckDB. All columns are read as text, so nothing
is silently coerced: values that fail to parse are counted, and rows the CSV
parser rejects are captured instead of dropped. An independent newline count
cross-checks the row count. Per-file results are cached, so an interrupted run
resumes where it stopped.

Run from the repo root:
    .venv/Scripts/python -m pipeline.inventory scan        # one pass per CSV (cached)
    .venv/Scripts/python -m pipeline.inventory boundaries  # duplicates between consecutive files
    .venv/Scripts/python -m pipeline.inventory report      # merged tables + report.md

Environment (optional): FIRE_DATA_DIR, FIRE_PROCESSED_DIR, DUCKDB_MEMORY_LIMIT (2GB), DUCKDB_THREADS (4).
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import os
import re
import statistics
import time
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("FIRE_DATA_DIR", REPO / "data"))
PROCESSED_DIR = Path(os.environ.get("FIRE_PROCESSED_DIR", DATA_DIR / "processed"))
OUT_DIR = PROCESSED_DIR / "inventory"
PER_FILE_DIR = OUT_DIR / "per_file"
TMP_DIR = OUT_DIR / "_duckdb_tmp"
MEMORY_LIMIT = os.environ.get("DUCKDB_MEMORY_LIMIT", "2GB")
THREADS = int(os.environ.get("DUCKDB_THREADS", "4"))

# Raw folder -> FIRMS product code (as it appears in the file names) and sensor.
SOURCES = {
    "MODIS": {"code": "M-C61", "sensor": "MODIS", "label": "MODIS Terra + Aqua"},
    "SV_C2": {"code": "SV-C2", "sensor": "VIIRS", "label": "VIIRS S-NPP"},
    "J1V_C2": {"code": "J1V-C2", "sensor": "VIIRS", "label": "VIIRS NOAA-20"},
    "J2V_C2": {"code": "J2V-C2", "sensor": "VIIRS", "label": "VIIRS NOAA-21"},
}
FILE_RE = re.compile(r"^fire_(?P<kind>archive|nrt)_(?P<code>.+)_(?P<id>\d+)\.csv$")
EXPECTED_COLS = [
    "latitude", "longitude", "brightness", "scan", "track", "acq_date", "acq_time",
    "satellite", "instrument", "confidence", "version", "bright_t31", "frp", "daynight",
]
OPTIONAL_COLS = ["type"]  # present in SP (archive) files only
NUM_COLS = {  # raw column -> short alias used in the statistics
    "latitude": "lat", "longitude": "lon", "brightness": "bri", "bright_t31": "t31",
    "scan": "scan", "track": "track", "frp": "frp",
}
SAT_ORDER = ["Terra", "Aqua", "SNPP", "N20", "N21"]
LOW_DAY_RATIO = 0.5  # a day is "low" when below this share of the centred 31-day median

# Same buckets FIRMS documents for MODIS (low < 30 <= nominal < 80 <= high); VIIRS is already l/n/h.
CONF_CLASS_SQL = """CASE
    WHEN TRY_CAST(confidence AS INTEGER) IS NOT NULL THEN
        CASE WHEN TRY_CAST(confidence AS INTEGER) < 30 THEN 'low'
                WHEN TRY_CAST(confidence AS INTEGER) < 80 THEN 'nominal' ELSE 'high' END
    WHEN lower(confidence) IN ('l', 'low') THEN 'low'
    WHEN lower(confidence) IN ('n', 'nominal') THEN 'nominal'
    WHEN lower(confidence) IN ('h', 'high') THEN 'high'
    ELSE 'other' END"""


# ----------------------------------------------------------------------------- helpers
def sql_path(p: Path) -> str:
    return p.as_posix().replace("'", "''")


def connect() -> duckdb.DuckDBPyConnection:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(":memory:")
    con.execute(f"SET memory_limit='{MEMORY_LIMIT}'")
    con.execute(f"SET threads={THREADS}")
    con.execute(f"SET temp_directory='{sql_path(TMP_DIR)}'")
    con.execute("SET preserve_insertion_order=false")
    return con


def jsonable(v):
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if v is None or isinstance(v, (int, float, str, bool)):
        return v
    return float(v) if "." in str(v) else int(v)  # Decimal / hugeint


def fetch_dict(con, sql: str) -> dict:
    cur = con.execute(sql)
    row = cur.fetchone()
    return {d[0]: jsonable(v) for d, v in zip(cur.description, row)}


def discover() -> list[dict]:
    """List the raw CSVs. Stops on anything that does not follow the FIRMS naming."""
    files = []
    for folder, meta in SOURCES.items():
        d = DATA_DIR / folder
        if not d.is_dir():
            raise SystemExit(f"Missing folder: {d}")
        for p in sorted(d.iterdir()):
            if not p.is_file():
                continue
            m = FILE_RE.match(p.name)
            if not m or m["code"] != meta["code"]:
                raise SystemExit(f"Unexpected file in {folder} (not a FIRMS {meta['code']} CSV): {p.name}")
            st = p.stat()
            files.append({
                "folder": folder, "file": p.name, "path": str(p), "code": meta["code"],
                "sensor": meta["sensor"], "processing": "SP" if m["kind"] == "archive" else "NRT",
                "file_id": m["id"], "size_bytes": st.st_size, "mtime": int(st.st_mtime),
            })
    return files


def read_edges(path: Path) -> tuple[list[str], str, str]:
    """Header columns, first data row and last data row, without reading the file."""
    with open(path, "rb") as f:
        header = f.readline().decode("utf-8").rstrip("\r\n")
        first = f.readline().decode("utf-8").rstrip("\r\n")
        size = f.seek(0, os.SEEK_END)
        f.seek(max(0, size - 4096))
        tail = f.read().decode("utf-8", errors="replace").splitlines()
    last = next((ln for ln in reversed(tail) if ln.strip()), "")
    return header.split(","), first, last


def count_newlines(path: Path) -> tuple[int, int, bool]:
    """Independent row count: number of LF and CR bytes, and whether the file ends with LF."""
    n_lf = n_cr = 0
    last = b""
    with open(path, "rb", buffering=0) as f:
        while chunk := f.read(1 << 24):
            n_lf += chunk.count(b"\n")
            n_cr += chunk.count(b"\r")
            last = chunk[-1:]
    return n_lf, n_cr, last == b"\n"


def csv_source(path: Path, header: list[str], rejects: bool = True) -> str:
    cols = ", ".join(f"'{c}': 'VARCHAR'" for c in header)
    opts = f"header=true, auto_detect=false, delim=',', quote='\"', escape='\"', nullstr='', columns={{{cols}}}"
    if rejects:
        opts += ", store_rejects=true"
    return f"read_csv('{sql_path(path)}', {opts})"


def check_header(f: dict, header: list[str]) -> None:
    extra = [c for c in header if c not in EXPECTED_COLS + OPTIONAL_COLS]
    missing = [c for c in EXPECTED_COLS if c not in header]
    if extra or missing or len(set(header)) != len(header):
        raise SystemExit(
            f"Unexpected header in {f['folder']}/{f['file']}: missing={missing} extra={extra} header={header}"
        )


# ----------------------------------------------------------------------------- scan
def cube_sql(src: str, has_type: bool) -> str:
    """Single pass: finest useful grouping, every other table is a roll-up of it."""
    values = ", ".join(
        f"CASE WHEN isfinite(TRY_CAST({raw} AS DOUBLE)) THEN TRY_CAST({raw} AS DOUBLE) END AS v_{a}"
        for raw, a in NUM_COLS.items()
    )
    aggs = []
    for raw, a in NUM_COLS.items():
        aggs += [
            f"count(*) FILTER (WHERE {raw} IS NULL) AS {a}_null",
            f"count(*) FILTER (WHERE {raw} IS NOT NULL AND v_{a} IS NULL) AS {a}_bad",
            f"min(v_{a}) AS {a}_min",
            f"max(v_{a}) AS {a}_max",
        ]
    aggs += [
        "sum(v_frp) AS frp_sum",
        "count(*) FILTER (WHERE v_frp <= 0) AS frp_nonpos",
        "count(*) FILTER (WHERE v_lat < -90 OR v_lat > 90) AS lat_oor",
        "count(*) FILTER (WHERE v_lon < -180 OR v_lon > 180) AS lon_oor",
        "max(len(split_part(latitude, '.', 2))) AS lat_dec_max",
        "max(len(split_part(longitude, '.', 2))) AS lon_dec_max",
        "count(*) FILTER (WHERE acq_time IS NULL) AS time_null",
        "count(*) FILTER (WHERE acq_time IS NOT NULL AND NOT "
        "regexp_full_match(acq_time, '([01][0-9]|2[0-3])[0-5][0-9]')) AS time_bad",
        "min(acq_time) AS time_min",
        "max(acq_time) AS time_max",
    ]
    type_col = "" if has_type else ', NULL::VARCHAR AS "type"'
    # Two groupings in the same pass: the daily cube (no_band = 1) and a coarse
    # year x 10-degree latitude band profile (no_band = 0).
    return f"""
        CREATE OR REPLACE TEMP TABLE cube_all AS
        SELECT acq_date, satellite, instrument, version, daynight, confidence, "type", yr, lat_band,
               GROUPING(lat_band) AS no_band,
               count(*) AS n, {", ".join(aggs)}
        FROM (SELECT *{type_col}, {values},
                     TRY_CAST(substr(acq_date, 1, 4) AS INTEGER) AS yr,
                     TRY_CAST(floor(TRY_CAST(latitude AS DOUBLE) / 10) * 10 AS INTEGER) AS lat_band
              FROM {src})
        GROUP BY GROUPING SETS (
            (acq_date, satellite, instrument, version, daynight, confidence, "type"),
            (yr, satellite, daynight, "type", lat_band)
        );
        CREATE OR REPLACE TEMP VIEW cube AS SELECT * FROM cube_all WHERE no_band = 1;
    """


def summary_sql() -> str:
    parts = [
        "coalesce(sum(n), 0)::BIGINT AS n_rows",
        "min(TRY_CAST(acq_date AS DATE)) AS date_min",
        "max(TRY_CAST(acq_date AS DATE)) AS date_max",
        "count(DISTINCT acq_date) AS n_days",
        "coalesce(sum(n) FILTER (WHERE TRY_CAST(acq_date AS DATE) IS NULL), 0)::BIGINT AS date_bad",
        "min(TRY_CAST(confidence AS INTEGER)) AS conf_num_min",
        "max(TRY_CAST(confidence AS INTEGER)) AS conf_num_max",
    ]
    for a in NUM_COLS.values():
        parts += [
            f"coalesce(sum({a}_null), 0)::BIGINT AS {a}_null",
            f"coalesce(sum({a}_bad), 0)::BIGINT AS {a}_bad",
            f"min({a}_min) AS {a}_min",
            f"max({a}_max) AS {a}_max",
        ]
    parts += [
        "sum(frp_sum) AS frp_sum",
        "coalesce(sum(frp_nonpos), 0)::BIGINT AS frp_nonpos",
        "coalesce(sum(lat_oor), 0)::BIGINT AS lat_oor",
        "coalesce(sum(lon_oor), 0)::BIGINT AS lon_oor",
        "max(lat_dec_max) AS lat_dec_max",
        "max(lon_dec_max) AS lon_dec_max",
        "coalesce(sum(time_null), 0)::BIGINT AS time_null",
        "coalesce(sum(time_bad), 0)::BIGINT AS time_bad",
        "(SELECT min(time_min) FROM cube WHERE acq_date = (SELECT min(acq_date) FROM cube)) AS start_time",
        "(SELECT max(time_max) FROM cube WHERE acq_date = (SELECT max(acq_date) FROM cube)) AS end_time",
        "min(time_min) AS time_min",
        "max(time_max) AS time_max",
    ]
    return f"SELECT {', '.join(parts)} FROM cube"


def counts_by(con, expr: str) -> dict:
    rows = con.execute(f"SELECT {expr} AS k, sum(n)::BIGINT FROM cube GROUP BY 1 ORDER BY 2 DESC").fetchall()
    return {("NULL" if k is None else str(k)): int(v) for k, v in rows}


def scan_file(f: dict) -> dict:
    path = Path(f["path"])
    stem = path.stem
    header, first_row, last_row = read_edges(path)
    check_header(f, header)
    has_type = "type" in header
    ident = f"'{f['folder']}' AS folder, '{f['file']}' AS file, '{f['processing']}' AS processing"

    t0 = time.perf_counter()
    con = connect()
    try:
        con.execute(cube_sql(csv_source(path, header), has_type))
        summary = fetch_dict(con, summary_sql())
        rejected_lines = con.execute("SELECT count(DISTINCT line) FROM reject_errors").fetchone()[0]
        reject_samples = [
            {"line": r[0], "column": r[1], "error": str(r[2]), "csv_line": r[3], "message": r[4]}
            for r in con.execute(
                "SELECT line, column_name, error_type, csv_line, error_message "
                "FROM reject_errors ORDER BY line LIMIT 5"
            ).fetchall()
        ]
        dims = {
            "by_satellite": counts_by(con, "satellite"),
            "by_instrument": counts_by(con, "instrument"),
            "by_version": counts_by(con, "version"),
            "by_daynight": counts_by(con, "daynight"),
            "by_conf_class": counts_by(con, CONF_CLASS_SQL),
            "by_type": counts_by(con, '"type"'),
        }
        con.execute(f"""
            COPY (
                SELECT {ident}, TRY_CAST(acq_date AS DATE) AS date, satellite, instrument, version,
                       daynight, {CONF_CLASS_SQL} AS conf_class, TRY_CAST("type" AS TINYINT) AS "type",
                       sum(n)::BIGINT AS n, sum(frp_sum) AS frp_sum
                FROM cube GROUP BY ALL ORDER BY date, satellite, daynight, conf_class, "type"
            ) TO '{sql_path(PER_FILE_DIR / (stem + ".daily.parquet"))}' (FORMAT parquet, COMPRESSION zstd)
        """)
        con.execute(f"""
            COPY (
                SELECT {ident}, satellite, instrument, version, daynight, confidence, "type",
                       sum(n)::BIGINT AS n, min(TRY_CAST(acq_date AS DATE)) AS date_min,
                       max(TRY_CAST(acq_date AS DATE)) AS date_max
                FROM cube GROUP BY ALL ORDER BY ALL
            ) TO '{sql_path(PER_FILE_DIR / (stem + ".values.parquet"))}' (FORMAT parquet, COMPRESSION zstd)
        """)
        con.execute(f"""
            COPY (
                SELECT {ident}, yr AS year, satellite, daynight, TRY_CAST("type" AS TINYINT) AS "type",
                       lat_band, n::BIGINT AS n, frp_sum
                FROM cube_all WHERE no_band = 0 ORDER BY ALL
            ) TO '{sql_path(PER_FILE_DIR / (stem + ".latband.parquet"))}' (FORMAT parquet, COMPRESSION zstd)
        """)
    finally:
        con.close()
    t_scan = time.perf_counter() - t0

    t0 = time.perf_counter()
    n_lf, n_cr, ends_lf = count_newlines(path)
    t_lines = time.perf_counter() - t0
    rows_by_newlines = n_lf - 1 if ends_lf else n_lf  # minus the header line

    rec = {k: v for k, v in f.items() if k != "path"}
    rec.update({
        "header": header, "n_columns": len(header), "has_type": has_type,
        "first_row": first_row, "last_row": last_row,
        "newlines": n_lf, "carriage_returns": n_cr, "ends_with_newline": ends_lf,
        "rows_by_newlines": rows_by_newlines, "rejected_lines": rejected_lines,
        "rows_match": rows_by_newlines == summary["n_rows"] + rejected_lines,
        "reject_samples": reject_samples,
        **summary, **dims,
        "first_row_is_min_date": first_row.split(",")[header.index("acq_date")] == summary["date_min"],
        "last_row_is_max_date": last_row.split(",")[header.index("acq_date")] == summary["date_max"],
        "seconds_scan": round(t_scan, 1), "seconds_newlines": round(t_lines, 1),
    })
    (PER_FILE_DIR / (stem + ".json")).write_text(json.dumps(rec, indent=1), encoding="utf-8")
    return rec


def load_cached(f: dict) -> dict | None:
    p = PER_FILE_DIR / (Path(f["file"]).stem + ".json")
    if not p.exists():
        return None
    rec = json.loads(p.read_text(encoding="utf-8"))
    same = rec.get("size_bytes") == f["size_bytes"] and rec.get("mtime") == f["mtime"]
    return rec if same else None


def cmd_scan(args) -> None:
    PER_FILE_DIR.mkdir(parents=True, exist_ok=True)
    files = discover()
    if args.only:
        files = [f for f in files if any(fnmatch.fnmatch(f["file"], pat) for pat in args.only)]
    todo = [f for f in files if args.force or load_cached(f) is None]
    total_gb = sum(f["size_bytes"] for f in todo) / 1e9
    print(f"{len(files)} files selected, {len(files) - len(todo)} cached, {len(todo)} to scan "
          f"({total_gb:.1f} GB) | memory_limit={MEMORY_LIMIT} threads={THREADS}", flush=True)
    done_gb, t_start = 0.0, time.perf_counter()
    for i, f in enumerate(todo, 1):
        rec = scan_file(f)
        done_gb += f["size_bytes"] / 1e9
        elapsed = time.perf_counter() - t_start
        eta = elapsed / done_gb * (total_gb - done_gb) if done_gb else 0
        flags = "" if rec["rows_match"] and not rec["rejected_lines"] else "  <-- CHECK rows/rejects"
        print(f"[{i}/{len(todo)}] {f['folder']}/{f['file']} {f['size_bytes'] / 1e6:,.0f} MB | "
              f"{rec['n_rows']:,} rows | {rec['date_min']} {rec['start_time']} -> {rec['date_max']} "
              f"{rec['end_time']} | scan {rec['seconds_scan']}s + lines {rec['seconds_newlines']}s | "
              f"elapsed {elapsed / 60:.1f} min, ETA {eta / 60:.1f} min{flags}", flush=True)
    print(f"Scan finished in {(time.perf_counter() - t_start) / 60:.1f} min", flush=True)


# ----------------------------------------------------------------------------- boundaries
def load_all() -> list[dict]:
    recs = [load_cached(f) for f in discover()]
    missing = sum(r is None for r in recs)
    if missing:
        print(f"WARNING: {missing} file(s) not scanned yet; the output below is partial.", flush=True)
    return [r for r in recs if r is not None]


def instant(date: str, hhmm: str) -> str:
    return f"{date} {hhmm[:2]}:{hhmm[2:]}"


def cmd_boundaries(args) -> None:
    """Consecutive files of a folder: gap between them and, if they overlap in time, duplicated rows."""
    recs = load_all()
    key_cols = ["latitude", "longitude", "acq_date", "acq_time", "satellite"]
    out = []
    for folder in SOURCES:
        fl = sorted((r for r in recs if r["folder"] == folder),
                    key=lambda r: (r["date_min"], r["start_time"], r["processing"] == "NRT"))
        for a, b in zip(fl, fl[1:]):
            a_end, b_start = instant(a["date_max"], a["end_time"]), instant(b["date_min"], b["start_time"])
            rec = {
                "folder": folder, "file_a": a["file"], "file_b": b["file"],
                "a_end": a_end, "b_start": b_start,
                "gap_days": (dt.date.fromisoformat(b["date_min"]) - dt.date.fromisoformat(a["date_max"])).days,
                "time_overlap": a_end >= b_start,
            }
            if rec["time_overlap"]:  # same instants may be in both files: compare the rows
                lo, hi = b["date_min"], a["date_max"]
                common = [c for c in a["header"] if c in b["header"]]
                con = connect()
                try:
                    for name, r in (("a", a), ("b", b)):
                        src = csv_source(DATA_DIR / folder / r["file"], r["header"], rejects=False)
                        con.execute(f"CREATE TEMP TABLE {name} AS SELECT {', '.join(common)} FROM {src} "
                                    f"WHERE acq_date BETWEEN '{lo}' AND '{hi}'")
                    keys = ", ".join(key_cols)
                    rec.update(fetch_dict(con, f"""
                        SELECT (SELECT count(*) FROM a) AS rows_a, (SELECT count(*) FROM b) AS rows_b,
                               (SELECT count(*) FROM (SELECT * FROM a INTERSECT SELECT * FROM b)) AS identical_rows,
                               (SELECT count(*) FROM (SELECT {keys} FROM a INTERSECT SELECT {keys} FROM b)) AS same_key_rows,
                               (SELECT min(acq_date || ' ' || acq_time) FROM b) AS overlap_from,
                               (SELECT max(acq_date || ' ' || acq_time) FROM a) AS overlap_to
                    """))
                finally:
                    con.close()
            out.append(rec)
            if rec["gap_days"] != 1 or rec["time_overlap"]:
                print(json.dumps(rec), flush=True)
    (OUT_DIR / "boundaries.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    shared = sum(r["gap_days"] == 0 for r in out)
    print(f"{len(out)} consecutive pairs | {shared} share a boundary day | "
          f"{sum(r['time_overlap'] for r in out)} overlap in time | "
          f"{sum(r['gap_days'] > 1 for r in out)} with a gap > 1 day", flush=True)


# ----------------------------------------------------------------------------- report
def n_fmt(x) -> str:
    return "-" if x is None else f"{x:,}"


def pct(part, total) -> str:
    return "-" if not total else f"{100 * (part or 0) / total:.2f}"


def md_table(headers: list[str], rows: list[list]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(lines) + "\n"


def sat_rank(s) -> tuple:
    return (SAT_ORDER.index(s) if s in SAT_ORDER else 99, str(s))


def sat_sort(sats) -> list[str]:
    return sorted(sats, key=sat_rank)


def date_ranges(days: list[dt.date]) -> list[tuple[dt.date, dt.date]]:
    out: list[list[dt.date]] = []
    for d in days:
        if out and (d - out[-1][1]).days == 1:
            out[-1][1] = d
        else:
            out.append([d, d])
    return [(a, b) for a, b in out]


def range_text(ranges) -> str:
    return ", ".join(f"{a}" if a == b else f"{a} → {b} ({(b - a).days + 1} d)" for a, b in ranges) or "none"


def cmd_report(args) -> None:
    recs = load_all()
    con = connect()
    glob = sql_path(PER_FILE_DIR)
    con.execute(f"CREATE TEMP TABLE daily AS SELECT * FROM read_parquet('{glob}/*.daily.parquet')")
    con.execute(f"CREATE TEMP TABLE vals AS SELECT * FROM read_parquet('{glob}/*.values.parquet')")
    con.execute(f"COPY (SELECT * FROM daily ORDER BY folder, date, file, satellite, daynight, conf_class, \"type\") "
                f"TO '{sql_path(OUT_DIR / 'daily_counts.parquet')}' (FORMAT parquet, COMPRESSION zstd)")
    con.execute(f"COPY (SELECT * FROM vals ORDER BY ALL) "
                f"TO '{sql_path(OUT_DIR / 'value_counts.parquet')}' (FORMAT parquet, COMPRESSION zstd)")
    (OUT_DIR / "files.json").write_text(json.dumps(recs, indent=1), encoding="utf-8")

    md = ["# FIRMS raw data inventory (generated by `python -m pipeline.inventory report`)\n",
          f"Generated {dt.datetime.now():%Y-%m-%d %H:%M}. {len(recs)} files, "
          f"{sum(r['size_bytes'] for r in recs) / 1e9:.2f} GB, {sum(r['n_rows'] for r in recs):,} rows.\n"]

    # 1. totals per folder and processing
    rows = []
    for folder, meta in SOURCES.items():
        for proc in ("SP", "NRT"):
            g = [r for r in recs if r["folder"] == folder and r["processing"] == proc]
            if not g:
                continue
            first = min(g, key=lambda r: (r["date_min"], r["start_time"]))
            last = max(g, key=lambda r: (r["date_max"], r["end_time"]))
            rows.append([f"`{folder}/`", meta["label"], proc, len(g), f"{sum(r['size_bytes'] for r in g) / 1e9:.2f}",
                         n_fmt(sum(r["n_rows"] for r in g)), instant(first["date_min"], first["start_time"]),
                         instant(last["date_max"], last["end_time"])])
    rows.append(["**Total**", "", "", len(recs), f"{sum(r['size_bytes'] for r in recs) / 1e9:.2f}",
                 n_fmt(sum(r["n_rows"] for r in recs)), "", ""])
    md += ["## 1. Totals per folder and processing level\n",
           md_table(["Folder", "Source", "Proc.", "Files", "GB", "Rows", "First detection (UTC)", "Last detection (UTC)"], rows)]

    # 2. per file
    rows = []
    for r in sorted(recs, key=lambda r: (list(SOURCES).index(r["folder"]), r["date_min"], r["processing"] == "NRT")):
        check = "ok" if r["rows_match"] and not r["rejected_lines"] else f"**CHECK** ({r['rejected_lines']} rejected)"
        rows.append([r["folder"], r["file_id"], r["processing"], f"{r['size_bytes'] / 1e6:,.0f}", n_fmt(r["n_rows"]),
                     instant(r["date_min"], r["start_time"]), instant(r["date_max"], r["end_time"]), r["n_days"],
                     " ".join(f"{k} {pct(v, r['n_rows'])}%" for k, v in r["by_satellite"].items()),
                     " / ".join(r["by_version"]), "yes" if r["has_type"] else "no", check])
    md += ["## 2. Per file\n",
           "`Rows = newlines` compares the DuckDB row count (+ rejected lines) with an independent newline count.\n",
           md_table(["Folder", "Id", "Proc.", "MB", "Rows", "From (UTC)", "To (UTC)", "Days", "Satellite share",
                     "Version(s)", "`type` col.", "Rows = newlines"], rows)]

    # 3. rows per calendar year and satellite
    data = con.execute("SELECT year(date), satellite, sum(n)::BIGINT FROM daily GROUP BY ALL").fetchall()
    sats = sat_sort({s for _, s, _ in data})
    by_year: dict = {}
    for y, s, n in data:
        by_year.setdefault(y, {})[s] = n
    rows = [[y] + [n_fmt(by_year[y].get(s)) for s in sats] + [n_fmt(sum(by_year[y].values()))]
            for y in sorted(by_year, key=lambda v: (v is None, v))]
    rows.append(["**Total**"] + [n_fmt(sum(v.get(s, 0) for v in by_year.values())) for s in sats]
                + [n_fmt(sum(sum(v.values()) for v in by_year.values()))])
    md += ["## 3. Rows per calendar year and satellite (all types, all confidences, SP + NRT)\n",
           md_table(["Year"] + sats + ["Total"], rows)]

    # 3b. night share per year and satellite (shows the Terra / Aqua orbit drift)
    data = con.execute("SELECT year(date), satellite, sum(n) FILTER (WHERE daynight = 'N'), sum(n) "
                       "FROM daily WHERE date IS NOT NULL GROUP BY ALL").fetchall()
    night = {(y, s): pct(nn, n) for y, s, nn, n in data}
    md += ["### Night share per calendar year and satellite (% of rows with `daynight = N`)\n",
           md_table(["Year"] + sats, [[y] + [night.get((y, s), "-") for s in sats]
                                      for y in sorted({y for y, _ in night})])]

    # 4. distributions per satellite and processing
    data = con.execute("""
        SELECT satellite, processing, sum(n)::BIGINT,
               sum(n) FILTER (WHERE daynight = 'D'), sum(n) FILTER (WHERE daynight = 'N'),
               sum(n) FILTER (WHERE conf_class = 'low'), sum(n) FILTER (WHERE conf_class = 'nominal'),
               sum(n) FILTER (WHERE conf_class = 'high'), sum(n) FILTER (WHERE conf_class = 'other'),
               sum(n) FILTER (WHERE "type" = 0), sum(n) FILTER (WHERE "type" = 1),
               sum(n) FILTER (WHERE "type" = 2), sum(n) FILTER (WHERE "type" = 3),
               sum(n) FILTER (WHERE "type" IS NULL), sum(frp_sum) / sum(n)
        FROM daily GROUP BY ALL
    """).fetchall()
    data.sort(key=lambda r: (sat_rank(r[0]), r[1] == "NRT"))
    rows = [[s, p, n_fmt(n), pct(d, n), pct(ni, n), pct(lo, n), pct(no, n), pct(hi, n), n_fmt(int(ot or 0)),
             pct(t0, n), pct(t1, n), pct(t2, n), pct(t3, n), pct(tn, n), f"{frp:.2f}"]
            for s, p, n, d, ni, lo, no, hi, ot, t0, t1, t2, t3, tn, frp in data]
    md += ["## 4. Value distributions per satellite and processing level (% of rows)\n",
           "MODIS confidence is bucketed as low < 30 ≤ nominal < 80 ≤ high. `type` is empty in NRT (no column).\n",
           md_table(["Satellite", "Proc.", "Rows", "Day", "Night", "Conf. low", "Conf. nominal", "Conf. high",
                     "Conf. other (rows)", "type 0", "type 1", "type 2", "type 3", "no type", "Mean FRP (MW)"], rows)]

    # 5. raw categorical values
    data = con.execute("""
        SELECT folder, satellite, instrument, version, processing, sum(n)::BIGINT, min(date_min), max(date_max)
        FROM vals GROUP BY ALL ORDER BY folder, satellite, min(date_min), version
    """).fetchall()
    data.sort(key=lambda r: list(SOURCES).index(r[0]))
    md += ["## 5. Raw categorical values\n", "### satellite / instrument / version\n",
           md_table(["Folder", "`satellite`", "`instrument`", "`version`", "Proc.", "Rows", "First date", "Last date"],
                    [[r[0], f"`{r[1]}`", f"`{r[2]}`", f"`{r[3]}`", r[4], n_fmt(r[5]), r[6], r[7]] for r in data])]
    data = con.execute("""
        SELECT folder, CASE WHEN TRY_CAST(confidence AS INTEGER) IS NOT NULL THEN '(integer)'
                            ELSE coalesce(confidence, 'NULL') END AS k, sum(n)::BIGINT,
               min(TRY_CAST(confidence AS INTEGER)), max(TRY_CAST(confidence AS INTEGER)),
               count(DISTINCT confidence)
        FROM vals GROUP BY ALL ORDER BY ALL
    """).fetchall()
    data.sort(key=lambda r: list(SOURCES).index(r[0]))
    md += ["### confidence (raw)\n",
           md_table(["Folder", "Value", "Rows", "Min", "Max", "Distinct values"],
                    [[r[0], f"`{r[1]}`", n_fmt(r[2]), n_fmt(r[3]), n_fmt(r[4]), r[5]] for r in data])]
    data = con.execute("""
        SELECT folder, processing, coalesce(daynight, 'NULL'), coalesce("type", 'NULL'), sum(n)::BIGINT
        FROM vals GROUP BY ALL ORDER BY ALL
    """).fetchall()
    data.sort(key=lambda r: list(SOURCES).index(r[0]))
    md += ["### daynight × type (raw; `NULL` type = column absent in NRT, or empty field)\n",
           md_table(["Folder", "Proc.", "`daynight`", "`type`", "Rows"],
                    [[r[0], r[1], f"`{r[2]}`", f"`{r[3]}`", n_fmt(r[4])] for r in data])]

    # 6. numeric ranges and data quality per folder and processing
    rows, qrows = [], []
    for folder in SOURCES:
        for proc in ("SP", "NRT"):
            g = [r for r in recs if r["folder"] == folder and r["processing"] == proc]
            if not g:
                continue
            lo = lambda k: min(r[k] for r in g if r[k] is not None)  # noqa: E731
            hi = lambda k: max(r[k] for r in g if r[k] is not None)  # noqa: E731
            tot = lambda k: sum(r[k] for r in g)  # noqa: E731
            rows.append([folder, proc] + [f"{lo(a + '_min'):.5f}".rstrip("0").rstrip(".") + " … "
                                          + f"{hi(a + '_max'):.5f}".rstrip("0").rstrip(".") for a in NUM_COLS.values()]
                        + [f"{lo('time_min')} … {hi('time_max')}", f"{hi('lat_dec_max')} / {hi('lon_dec_max')}"])
            qrows.append([folder, proc, n_fmt(tot("rejected_lines")),
                          n_fmt(sum(tot(a + "_null") for a in NUM_COLS.values()) + tot("time_null")),
                          n_fmt(sum(tot(a + "_bad") for a in NUM_COLS.values()) + tot("time_bad") + tot("date_bad")),
                          n_fmt(tot("lat_oor") + tot("lon_oor")), n_fmt(tot("frp_null")), n_fmt(tot("frp_nonpos")),
                          n_fmt(sum(r["carriage_returns"] for r in g)),
                          "yes" if all(r["rows_match"] for r in g) else "**NO**",
                          "yes" if all(r["first_row_is_min_date"] and r["last_row_is_max_date"] for r in g) else "**NO**"])
    md += ["## 6. Numeric ranges and data quality\n",
           md_table(["Folder", "Proc."] + [f"`{c}`" for c in NUM_COLS] + ["`acq_time`", "lat / lon decimals"], rows),
           "\n",
           md_table(["Folder", "Proc.", "Rejected CSV lines", "Empty numeric/time fields", "Unparseable values",
                     "lat/lon out of range", "`frp` empty", "`frp` ≤ 0", "CR bytes", "Rows = newlines",
                     "First/last row = min/max date"], qrows)]

    # 7. SP -> NRT hand-over
    both = dict(con.execute("""
        SELECT folder, count(*) FROM (SELECT folder, date FROM daily GROUP BY ALL
                                      HAVING count(DISTINCT processing) > 1) GROUP BY ALL
    """).fetchall())
    rows = []
    for folder in SOURCES:
        sp = [r for r in recs if r["folder"] == folder and r["processing"] == "SP"]
        nrt = [r for r in recs if r["folder"] == folder and r["processing"] == "NRT"]
        if not sp or not nrt:
            continue
        s = max(sp, key=lambda r: (r["date_max"], r["end_time"]))
        n0 = min(nrt, key=lambda r: (r["date_min"], r["start_time"]))
        n1 = max(nrt, key=lambda r: (r["date_max"], r["end_time"]))
        rows.append([folder, instant(s["date_max"], s["end_time"]), instant(n0["date_min"], n0["start_time"]),
                     instant(n1["date_max"], n1["end_time"]), both.get(folder, 0)])
    md += ["## 7. SP → NRT hand-over\n",
           md_table(["Folder", "Last SP detection", "First NRT detection", "Last NRT detection",
                     "Days with both SP and NRT rows"], rows)]

    # 8. boundaries between consecutive files
    bpath = OUT_DIR / "boundaries.json"
    if bpath.exists():
        bounds = json.loads(bpath.read_text(encoding="utf-8"))
        rows = [[b["folder"], Path(b["file_a"]).stem.split("_")[-1] + (" NRT" if "nrt" in b["file_a"] else ""),
                 Path(b["file_b"]).stem.split("_")[-1] + (" NRT" if "nrt" in b["file_b"] else ""),
                 b["a_end"], b["b_start"], b["gap_days"], "yes" if b["time_overlap"] else "no",
                 n_fmt(b.get("identical_rows")), n_fmt(b.get("same_key_rows"))]
                for b in bounds if b["gap_days"] != 1 or b["time_overlap"]]
        md += ["## 8. Boundaries between consecutive files\n",
               f"{len(bounds)} consecutive pairs checked. Listed: pairs that share a calendar day, overlap in time "
               "or leave a gap. Pairs not listed end on one day and resume on the next.\n",
               md_table(["Folder", "File A", "File B", "A ends (UTC)", "B starts (UTC)", "Gap (days)",
                         "Time overlap", "Identical rows", "Same lat/lon/date/time/satellite"], rows)
               if rows else "No shared days, overlaps or gaps.\n"]

    # 9. days without data and suspiciously low days, per satellite
    data = con.execute("SELECT satellite, date, sum(n)::BIGINT FROM daily WHERE date IS NOT NULL "
                       "GROUP BY ALL ORDER BY ALL").fetchall()
    per_sat: dict = {}
    for s, d, n in data:
        per_sat.setdefault(s, {})[d] = n
    rows, gaps = [], {}
    for s in sat_sort(per_sat):
        days = per_sat[s]
        first, last = min(days), max(days)
        span = [first + dt.timedelta(days=i) for i in range((last - first).days + 1)]
        counts = [days.get(d, 0) for d in span]
        missing = [d for d, c in zip(span, counts) if c == 0]
        low = []
        for i, (d, c) in enumerate(zip(span, counts)):
            med = statistics.median(counts[max(0, i - 15): i + 16])
            if 0 < c < LOW_DAY_RATIO * med:
                low.append(d)
        gaps[s] = {"first": str(first), "last": str(last), "days_in_span": len(span),
                   "missing": [str(d) for d in missing], "low": [str(d) for d in low]}
        rows.append([s, first, last, len(span), len(missing), range_text(date_ranges(missing)),
                     len(low), range_text(date_ranges(low))])
    (OUT_DIR / "gaps.json").write_text(json.dumps(gaps, indent=1), encoding="utf-8")
    md += ["## 9. Days without data and low days per satellite\n",
           f"A day is *low* when its global count is below {LOW_DAY_RATIO:.0%} of the centred 31-day median "
           "(indicative: possible partial outages).\n",
           md_table(["Satellite", "First day", "Last day", "Days in span", "Days with 0 rows", "Missing ranges",
                     "Low days", "Low ranges"], rows)]

    # 10. latitude profile
    con.execute(f"CREATE TEMP TABLE bands AS SELECT * FROM read_parquet('{glob}/*.latband.parquet')")
    con.execute(f"COPY (SELECT * FROM bands ORDER BY ALL) "
                f"TO '{sql_path(OUT_DIR / 'latband_counts.parquet')}' (FORMAT parquet, COMPRESSION zstd)")
    data = con.execute("SELECT lat_band, satellite, sum(n)::BIGINT FROM bands GROUP BY ALL").fetchall()
    sats = sat_sort({s for _, s, _ in data})
    by_band: dict = {}
    for b, s, n in data:
        by_band.setdefault(b, {})[s] = n
    rows = [["-" if b is None else f"{b}° … {b + 10}°"] + [n_fmt(by_band[b].get(s)) for s in sats]
            for b in sorted(by_band, key=lambda v: (v is None, -(v or 0)))]
    md += ["## 10. Rows per 10° latitude band and satellite (all years, all types)\n",
           md_table(["Latitude band"] + sats, rows)]
    data = con.execute("""
        SELECT CASE WHEN lat_band < -60 THEN 'south of 60°S' ELSE 'north of 80°N' END, satellite,
               coalesce("type"::VARCHAR, 'none (NRT)'), sum(n)::BIGINT, min(year), max(year)
        FROM bands WHERE lat_band < -60 OR lat_band >= 80 GROUP BY ALL ORDER BY ALL
    """).fetchall()
    md += ["### Polar detections (implausible for vegetation fires)\n",
           md_table(["Zone", "Satellite", "`type`", "Rows", "First year", "Last year"],
                    [[z, s, f"`{t}`", n_fmt(n), y0, y1] for z, s, t, n, y0, y1 in data])
           if data else "None.\n"]

    # 11. timings
    t_scan, t_lines = sum(r["seconds_scan"] for r in recs), sum(r["seconds_newlines"] for r in recs)
    md += ["## 11. Timings\n",
           f"DuckDB scan {t_scan / 60:.1f} min + newline count {t_lines / 60:.1f} min for "
           f"{sum(r['size_bytes'] for r in recs) / 1e9:.1f} GB (memory_limit={MEMORY_LIMIT}, threads={THREADS}).\n"]

    (OUT_DIR / "report.md").write_text("\n".join(md), encoding="utf-8")
    con.close()
    try:
        TMP_DIR.rmdir()
    except OSError:
        pass
    print(f"Wrote {OUT_DIR / 'report.md'} (+ files.json, daily_counts.parquet, value_counts.parquet, gaps.json)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan", help="one streamed pass per raw CSV (cached per file)")
    p.add_argument("--only", nargs="+", metavar="GLOB", help="file-name patterns, e.g. fire_nrt_*")
    p.add_argument("--force", action="store_true", help="rescan files that are already cached")
    p.set_defaults(func=cmd_scan)
    sub.add_parser("boundaries", help="gaps and duplicates between consecutive files").set_defaults(func=cmd_boundaries)
    sub.add_parser("report", help="merge the per-file results and write report.md").set_defaults(func=cmd_report)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
