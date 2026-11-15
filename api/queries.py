# SI-assisted: written with Claude Code (Anthropic LLM) under the author's direction and reviewed by the author.
"""DuckDB queries behind the API (global data).

Ported from the preparation project and adapted: no regions; one harmonized source per month (series_month);
the detections table is enriched at query time (pipeline.publish.points_sql) instead of a second points copy;
the last data month is partial and its anomaly uses a pro-rated baseline (user decision, step 8); months
with NRT data carry the warning that the detection type was calculated (user requirement, step 5).
"""
from __future__ import annotations

import datetime as dt
import io
import json
import math
import os
import threading
import warnings
from contextlib import contextmanager
from functools import lru_cache

import duckdb
import numpy as np

from pipeline import config, publish

_lock = threading.Lock()
_con = None
CSV_MAX_ROWS = int(os.getenv("CSV_MAX_ROWS", "1000000"))  # provisional; the final limit waits for step 10
UNIT = "VIIRS S-NPP equivalent detections (375 m), afternoon + night overpasses"
TYPE_NOTE = ("The detection type (vegetation fire, volcano, static source) of near-real-time data was calculated "
             "from each location's history: FIRMS did not deliver a type for these detections.")


class TooManyRows(Exception):
    pass


# Step 10e: Cloud Run cuts a request at 300 s but the DuckDB query keeps running inside the instance and slows every
# other request there. Queries over the detections are interrupted after this many seconds instead (main.py → 503).
QUERY_TIMEOUT_S = float(os.getenv("QUERY_TIMEOUT_S", "120"))


class QueryTimeout(Exception):
    pass


@contextmanager
def time_limit(cur, what: str = "The query"):
    """Interrupt `cur`'s queries inside the block after QUERY_TIMEOUT_S seconds (raises QueryTimeout).

    An interrupt that fires after the last query has finished does nothing (DuckDB does not remember it).
    """
    timer = threading.Timer(QUERY_TIMEOUT_S, cur.interrupt)
    timer.daemon = True
    timer.start()
    try:
        yield cur
    except duckdb.InterruptException:
        raise QueryTimeout(f"{what} took longer than {QUERY_TIMEOUT_S:g} s and was stopped: "
                           "narrow the dates or the area.") from None
    finally:
        timer.cancel()


def con():
    """Shared in-memory DuckDB database; each call returns a cursor (thread-safe usage)."""
    global _con
    with _lock:
        if _con is None:
            c = config.connect()
            # Cloud Run (step 10b): keep the Parquet footers of the detections files (read through the bucket
            # mount) in memory between queries. The block cache (`cache_local_files`) stays off (step 10e): it
            # splits reads into 16 KB pieces, about twice the read operations, each one a bucket request.
            c.execute("SET parquet_metadata_cache=true")
            c.execute("INSTALL spatial; LOAD spatial")
            h = config.sql_path(config.harmonized_path())
            c.execute(f"CREATE VIEW harmonized AS SELECT * FROM read_parquet('{h}')")
            publish.prepare(c)
            # Temp tables are private to one connection; cursors need them in the shared catalog.
            for t in ("static_mask", "series_month"):
                c.execute(f"CREATE TABLE main.{t} AS SELECT * FROM temp.{t}")
                c.execute(f"DROP TABLE temp.{t}")
            _con = c
        return _con.cursor()


# ----------------------------------------------------------------------------- metadata
@lru_cache(maxsize=1)
def meta() -> dict:
    c = con()
    months = c.execute("SELECT year, month, source, series_sat, days_fraction FROM series_month ORDER BY ym").fetchall()
    f = publish.FACTS  # serving_facts.json, loaded by publish.prepare() in con(): no scan at startup
    if f:
        last = dt.date.fromisoformat(f["last_date"])
        nrt_first = dt.date.fromisoformat(f["nrt_first"]) if f["nrt_first"] else None
        inferred = list(f["type_inferred_months"])
    else:  # facts file missing or stale: compute live
        daily = publish.daily_path()
        if daily.exists():
            d = config.sql_path(daily)
            last, nrt_first = c.execute(f"SELECT max(date), min(date) FILTER (WHERE type_inferred) FROM '{d}'").fetchone()
        else:
            last, nrt_first = publish.last_date(c), None
        inferred = [f"{y}-{m:02d}" for y, m in c.execute(
            "SELECT DISTINCT year, month FROM harmonized WHERE type_inferred ORDER BY 1, 2").fetchall()]
    partial = [r for r in months if r[4] < 1]
    p = None
    if partial:
        y, m, _, _, frac = partial[-1]
        p = {"month": f"{y}-{m:02d}", "days_fraction": frac, "days": last.day,
             "days_in_month": round(last.day / frac)}
    y0, m0 = months[0][:2]
    y1, m1 = months[-1][:2]
    return {
        "first_month": f"{y0}-{m0:02d}", "last_month": f"{y1}-{m1:02d}", "data_through": last.isoformat(),
        "partial_month": p, "type_inferred_months": inferred,
        "nrt_first": nrt_first.isoformat() if nrt_first else None,
        "type_inferred_note": TYPE_NOTE, "unit": UNIT,
    }


@lru_cache(maxsize=1)
def _months() -> dict[int, tuple[str, str, float]]:
    """ym (YYYYMM) -> (source, series satellite, days_fraction)."""
    return {y * 100 + m: (s, sat, f) for y, m, s, sat, f in
            con().execute("SELECT year, month, source, series_sat, days_fraction FROM series_month").fetchall()}


# ----------------------------------------------------------------------------- area handling
def _circle(lat: float, lon: float, r_km: float, n: int = 64) -> dict:
    dlat = r_km / 111.32
    dlon = r_km / (111.32 * max(math.cos(math.radians(lat)), 0.05))
    ring = [[lon + dlon * math.cos(2 * math.pi * i / n), lat + dlat * math.sin(2 * math.pi * i / n)] for i in range(n)]
    return {"type": "Polygon", "coordinates": [ring + [ring[0]]]}


def _box(x0: float, y0: float, x1: float, y1: float) -> list:
    return [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]


def geometry_geojson(geometry: dict | None, bbox: list[float] | None, point: dict | None) -> str:
    """Normalize the 3 supported area inputs to a GeoJSON geometry string."""
    if geometry:
        if geometry.get("type") == "Feature":
            geometry = geometry["geometry"]
        if geometry.get("type") not in ("Polygon", "MultiPolygon"):
            raise ValueError("geometry must be a Polygon or MultiPolygon")
        return json.dumps(geometry)
    if point:
        return json.dumps(_circle(point["lat"], point["lon"], point.get("radius_km", 25.0)))
    if bbox:
        x0, y0, x1, y1 = bbox
        if y0 >= y1:
            raise ValueError("bbox must be [min_lon, min_lat, max_lon, max_lat]")
        if x0 > x1:  # crosses the antimeridian
            return json.dumps({"type": "MultiPolygon", "coordinates": [_box(x0, y0, 180, y1), _box(-180, y0, x1, y1)]})
        return json.dumps({"type": "Polygon", "coordinates": _box(x0, y0, x1, y1)})
    raise ValueError("Provide geometry, bbox or point")


def _bounds(geojson: str) -> tuple[float, float, float, float]:
    """Bounding box from the coordinates (as literals in SQL, so DuckDB can prune row groups)."""
    xs, ys = [], []

    def walk(c):
        if c and isinstance(c[0], (int, float)):
            xs.append(float(c[0])); ys.append(float(c[1]))
        else:
            for x in c:
                walk(x)

    walk(json.loads(geojson)["coordinates"])
    if not xs:
        raise ValueError("empty geometry")
    return max(min(xs), -180.0), max(min(ys), -90.0), min(max(xs), 180.0), min(max(ys), 90.0)


def _cell_weights_cte(geojson: str) -> tuple[str, list]:
    """Cells of the harmonized grid touching the area, weighted by the share of the cell inside it.

    The area is a constant parameter ($1) rather than a cross-joined row, so DuckDB's spatial extension uses a
    prepared geometry for ST_Intersects / ST_Contains; the costly ST_Intersection only runs on border cells.
    (Step 9b: the cross-join version ran out of memory on Canada and took 14-21 s on the USA and Russia.)
    """
    g = config.SETTINGS.grid_deg
    x0, y0, x1, y1 = _bounds(geojson)
    sql = f"""
    cells AS (
      SELECT DISTINCT cell_lat, cell_lon FROM harmonized
      WHERE cell_lat BETWEEN {y0 - g} AND {y1} AND cell_lon BETWEEN {x0 - g} AND {x1}
    ),
    e AS (SELECT cell_lat, cell_lon, ST_MakeEnvelope(cell_lon, cell_lat, cell_lon + {g}, cell_lat + {g}) AS env
          FROM cells),
    k AS (
      SELECT cell_lat, cell_lon, env,
             ST_Intersects(ST_GeomFromGeoJSON($1), env) AS hit,
             ST_Contains(ST_GeomFromGeoJSON($1), env) AS inside
      FROM e
    ),
    w AS (
      SELECT cell_lat, cell_lon,
             CASE WHEN inside THEN 1.0
                  ELSE ST_Area(ST_Intersection(ST_GeomFromGeoJSON($1), env)) / ({g} * {g}) END AS weight
      FROM k WHERE hit
    )"""
    return sql, [geojson]


def monthly_series(geojson: str) -> list[tuple]:
    """(year, month, harmonized, terra, aqua, snpp) for the area, area-weighted."""
    cte, params = _cell_weights_cte(geojson)
    return con().execute(
        f"""
        WITH {cte}
        SELECT CAST(h.year AS INTEGER), CAST(h.month AS INTEGER),
               sum(h.n_harmonized * w.weight), sum(h.n_terra * w.weight),
               sum(h.n_aqua * w.weight), sum(h.n_snpp * w.weight)
        FROM harmonized h JOIN w USING (cell_lat, cell_lon)
        WHERE w.weight > 0
        GROUP BY ALL ORDER BY ALL
        """, params).fetchall()


def _ym(s: str | None, default: int) -> int:
    if not s:
        return default
    v = s.replace("-", "")[:6]
    if len(v) != 6 or not v.isdigit() or not 1 <= int(v[4:]) <= 12:
        raise ValueError("dates must be YYYY-MM or YYYY-MM-DD")
    return int(v)


# ----------------------------------------------------------------------------- calendar and anomaly
def calendar(geojson: str, start: str | None, end: str | None) -> dict:
    months = _months()
    lo, hi = min(months), max(months)
    s, e = max(_ym(start, lo), lo), min(_ym(end, hi), hi)
    if s > e:
        raise ValueError("start must be before end and inside the data period")
    inferred = set(meta()["type_inferred_months"])
    rows = [r for r in monthly_series(geojson) if s <= r[0] * 100 + r[1] <= e]
    y0, y1 = s // 100, e // 100
    years = list(range(y0, y1 + 1))
    shape = (len(years), 12)
    H, modis, viirs, frac = (np.full(shape, np.nan) for _ in range(4))
    src = [[None] * 12 for _ in years]
    tinf = [[False] * 12 for _ in years]
    # months inside the requested range but with no detections in any cell -> 0
    for yi, y in enumerate(years):
        for m in range(1, 13):
            ym = y * 100 + m
            if s <= ym <= e:
                H[yi, m - 1] = modis[yi, m - 1] = viirs[yi, m - 1] = 0.0
                src[yi][m - 1], _, frac[yi, m - 1] = months[ym]
                tinf[yi][m - 1] = f"{y}-{m:02d}" in inferred
    for y, m, h, t, a, v in rows:
        yi = y - y0
        H[yi, m - 1], viirs[yi, m - 1] = h, v
        modis[yi, m - 1] = a if y * 100 + m >= 200207 else t
    # Anomaly vs the multi-year median of the same month (complete months only); a partial month is compared
    # with the median pro-rated to its covered days.
    full = np.where(frac < 1, np.nan, H)
    with warnings.catch_warnings():  # months with no data at all in the range -> NaN
        warnings.simplefilter("ignore", RuntimeWarning)
        med = np.nanmedian(full, axis=0)
    expected = med[None, :] * frac
    anom_pct = 100 * (H - expected) / np.where(expected > 0, expected, np.nan)

    def clean(a, nd=2):
        return [[None if np.isnan(x) else round(float(x), nd) for x in row] for row in a]

    return {
        "years": years,
        "months": list(range(1, 13)),
        "harmonized": clean(H),
        "raw_modis": clean(modis),
        "raw_viirs": clean(viirs),
        "source": src,
        "days_fraction": clean(frac, 4),
        "type_inferred": tinf,
        "monthly_median": [None if np.isnan(x) else round(float(x), 2) for x in med],
        "anomaly_pct": clean(anom_pct),
        "year_totals": [round(float(x), 2) for x in np.nansum(H, axis=1)],
        "data_through": meta()["data_through"],
        "type_inferred_note": TYPE_NOTE,
        "unit": UNIT,
    }


def anomaly(geojson: str, year: int, month: int, baseline: tuple[int, int] | None) -> dict:
    months = _months()
    lo, hi = min(months), max(months)
    ym = year * 100 + month
    if not lo <= ym <= hi:
        raise ValueError(f"{year}-{month:02d} is outside the data period")
    vals = {(y, m): h for y, m, h, *_ in monthly_series(geojson)}
    frac = months[ym][2]
    by = baseline or (lo // 100, year - 1)
    hist = [vals.get((y, month), 0.0) for y in range(by[0], by[1] + 1)
            if y != year and y * 100 + month in months and months[y * 100 + month][2] == 1]
    if not hist:
        raise ValueError("No baseline years available")
    current = vals.get((year, month), 0.0)
    arr = np.array(hist) * frac  # pro-rated to the covered days when the month is partial
    p = {f"p{q}": round(float(np.percentile(arr, q)), 2) for q in (10, 25, 50, 75, 90)}
    med, mean = float(np.median(arr)), float(arr.mean())
    std = float(arr.std(ddof=1)) if len(arr) > 1 else 0.0
    return {
        "year": year, "month": month, "current": round(current, 2),
        "baseline_years": list(by), "n_baseline_years": len(hist),
        "median": round(med, 2), "mean": round(mean, 2), "std": round(std, 2), **p,
        "anomaly_abs": round(current - med, 2),
        "anomaly_pct_vs_median": round(100 * (current - med) / med, 1) if med > 0 else None,
        "anomaly_pct_vs_mean": round(100 * (current - mean) / mean, 1) if mean > 0 else None,
        "z_score": round((current - mean) / std, 2) if std > 0 else None,
        "percentile_rank": round(100 * float((arr < current).mean() + 0.5 * (arr == current).mean()), 1),
        "is_complete": frac == 1,
        "days_fraction": frac,
        "baseline_prorated": frac < 1,
        "data_through": meta()["data_through"],
        "type_inferred": f"{year}-{month:02d}" in set(meta()["type_inferred_months"]),
        "type_inferred_note": TYPE_NOTE,
        "source": months[ym][0],
    }


# ----------------------------------------------------------------------------- CSV
def _date(s: str | None) -> dt.date | None:
    if not s:
        return None
    try:
        return dt.date.fromisoformat(s if len(s) > 7 else s + "-01")
    except ValueError:
        raise ValueError("dates must be YYYY-MM-DD")


def hotspots_csv(geojson: str, start: str | None, end: str | None, only_calendar: bool) -> str:
    """Raw detections in the area with sensor, flags, type source and VIIRS-equivalent weight.

    The area is a constant parameter ($1), so ST_Contains uses a prepared geometry (fast for country borders).
    """
    g = config.SETTINGS.grid_deg
    cur = con()  # first: loads the series-month table behind points_sql()
    x0, y0, x1, y1 = _bounds(geojson)
    d0, d1 = _date(start), _date(end)
    where = [f"h.lon BETWEEN {x0} AND {x1}", f"h.lat BETWEEN {y0} AND {y1}"]
    hwhere = [f"z.cell_lat BETWEEN {y0 - g} AND {y1}", f"z.cell_lon BETWEEN {x0 - g} AND {x1}"]
    if d0:
        where += [f"h.date >= DATE '{d0}'", f"h.year >= {d0.year}"]
        hwhere.append(f"z.year >= {d0.year}")
    if d1:
        where += [f"h.date <= DATE '{d1}'", f"h.year <= {d1.year}"]
        hwhere.append(f"z.year <= {d1.year}")
    sql = f"""
    SELECT p.sensor, CASE p.satellite WHEN 'N' THEN 'S-NPP' WHEN 'N20' THEN 'NOAA-20' WHEN 'N21' THEN 'NOAA-21'
                     ELSE p.satellite END AS satellite,
           p.lat AS latitude, p.lon AS longitude,
           strftime(p.datetime_utc, '%Y-%m-%dT%H:%MZ') AS datetime_utc, p.daynight,
           p.confidence_raw, p.confidence_norm, p.frp, p.brightness, p.scan, p.track, p.version, p.processing,
           p.type, p.type_firms,
           CASE WHEN p.type_inferred THEN 'inferred' ELSE 'FIRMS' END AS type_source,
           p.in_calendar, round(p.weight, 4) AS viirs_equivalent_weight, p.harmonized_source
    FROM ({publish.points_sql(' AND '.join(where), ' AND '.join(hwhere), publish.hotspot_files(d0, d1))}) p
    WHERE ST_Contains(ST_GeomFromGeoJSON($1), ST_Point(p.lon, p.lat)) {'AND p.in_calendar' if only_calendar else ''}
    ORDER BY p.datetime_utc
    LIMIT {CSV_MAX_ROWS + 1}
    """
    with time_limit(cur, "The CSV query"):
        df = cur.execute(sql, [geojson]).df()
    if len(df) > CSV_MAX_ROWS:
        raise TooManyRows(f"The query returns more than {CSV_MAX_ROWS:,} detections: narrow the area or the dates.")
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    return buf.getvalue()
