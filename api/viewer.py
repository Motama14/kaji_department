# SI-assisted: written with Claude Code (Anthropic LLM) under the author's direction and reviewed by the author.
"""Endpoints for the world hotspot viewer (map + timeline).

Ported from the preparation project and adapted to the global data: no regions (the `region` parameter is
accepted for compatibility: `visible` is handled by the client through `bbox`); points come from the single
detections table enriched at query time (pipeline.publish.points_sql); the timeline's fast path reads
daily_counts.parquet. Points carry `ti` = 1 when their type was inferred (NRT data), for the warning; the `ti`
parameter (all | firms | inferred, step 9c) filters on it.
"""
from __future__ import annotations

import datetime as dt
from functools import lru_cache

from fastapi import APIRouter, HTTPException, Query

from pipeline import config, publish

from . import queries as q

router = APIRouter(prefix="/api")

SAT_CODES = {"terra": "Terra", "aqua": "Aqua", "snpp": "N", "n20": "N20", "n21": "N21"}
SAT_LABELS = {"Terra": "MODIS Terra", "Aqua": "MODIS Aqua", "N": "VIIRS S-NPP",
              "N20": "VIIRS NOAA-20", "N21": "VIIRS NOAA-21"}
SAT_INDEX = ["Terra", "Aqua", "N", "N20", "N21"]
CONF_INDEX = ["low", "nominal", "high"]
MAX_POINTS = 150_000  # above this, the map gets an aggregated grid instead of points


def _daily_src() -> str:
    p = publish.daily_path()
    if not p.exists():
        raise HTTPException(503, "daily_counts.parquet is missing: run `python -m pipeline.publish build`")
    return f"'{config.sql_path(p)}'"


def _points_src(d0: dt.date, d1: dt.date, bbox=None) -> str:
    q.con()  # makes sure the series-month table behind points_sql() is loaded
    where =[f"h.date BETWEEN DATE '{d0}' AND DATE '{d1}'", f"h.year BETWEEN {d0.year} AND {d1.year}"]
    # Plain column comparisons (no expressions) so DuckDB pushes them into the Parquet scan; the month list is a
    # superset when the window spans a year boundary, the join keeps the exact months.
    if (d1.year - d0.year) * 12 + d1.month - d0.month >= 11:
        months = range(1, 13)
    elif d0.year == d1.year:
        months = range(d0.month, d1.month + 1)
    else:
        months = [*range(d0.month, 13), *range(1, d1.month + 1)]
    hwhere = [f"z.year BETWEEN {d0.year} AND {d1.year}", f"z.month IN ({', '.join(map(str, months))})"]
    if bbox:
        x0, y0, x1, y1 = bbox
        where.append(f"h.lat BETWEEN {y0} AND {y1}")
        hwhere.append(f"z.cell_lat BETWEEN {y0 - config.SETTINGS.grid_deg} AND {y1}")
    # Only the detections files of these dates (no folder walk on the bucket mount, step 10c).
    return f"({publish.points_sql(' AND '.join(where), ' AND '.join(hwhere), publish.hotspot_files(d0, d1))})"


def _window(date: str, window: str) -> tuple[dt.date, dt.date]:
    try:
        d = dt.date.fromisoformat(date)
    except ValueError:
        raise HTTPException(422, "date must be YYYY-MM-DD")
    if window == "day":
        return d, d
    if window == "7d":
        return d - dt.timedelta(days=6), d
    if window == "month":
        start = d.replace(day=1)
        nxt = (start + dt.timedelta(days=32)).replace(day=1)
        return start, nxt - dt.timedelta(days=1)
    raise HTTPException(422, "window must be day, 7d or month")


def _csv(v: str | None) -> tuple[str, ...]:
    return tuple(sorted(x.strip() for x in v.split(",") if x.strip())) if v else ()


def _filters(sensors, mode, types, conf, daynight, bbox, frp_min, frp_max, hour_min, hour_max,
             ti="all") -> tuple[list[str], list]:
    """WHERE clauses shared by the points (enriched detections) and the daily counts table."""
    w, params = [], []
    if ti == "firms":  # step 9c: type delivered by FIRMS (SP data) vs calculated (NRT data)
        w.append("NOT type_inferred")
    elif ti == "inferred":
        w.append("type_inferred")
    if mode == "harmonized":
        w.append("series_sat")
    else:
        sats = [SAT_CODES[s] for s in sensors if s in SAT_CODES]
        w.append(f"satellite IN ({','.join('?' * len(sats)) or 'NULL'})"); params += sats
    try:
        t = [int(x) for x in types]
    except ValueError:
        raise HTTPException(422, "types must be integers")
    w.append(f"type IN ({','.join(map(str, t)) or 'NULL'})")
    c = [x for x in conf if x in CONF_INDEX]
    w.append(f"confidence_norm IN ({','.join('?' * len(c)) or 'NULL'})"); params += c
    dn = [x for x in daynight if x in ("D", "N")]
    w.append(f"daynight IN ({','.join('?' * len(dn)) or 'NULL'})"); params += dn
    if bbox:
        x0, y0, x1, y1 = bbox
        w.append("lat BETWEEN ? AND ?"); params += [y0, y1]
        if x0 <= x1:
            w.append("lon BETWEEN ? AND ?"); params += [x0, x1]
        else:  # crosses the antimeridian
            w.append("(lon >= ? OR lon <= ?)"); params += [x0, x1]
    if frp_min is not None:
        w.append("frp >= ?"); params.append(frp_min)
    if frp_max is not None:
        w.append("frp <= ?"); params.append(frp_max)
    if hour_min is not None and hour_min > 0:
        w.append("hour(datetime_utc) >= ?"); params.append(hour_min)
    if hour_max is not None and hour_max < 23:
        w.append("hour(datetime_utc) <= ?"); params.append(hour_max)
    return w, params


def _check_ti(ti: str) -> None:
    if ti not in ("all", "firms", "inferred"):
        raise HTTPException(422, "ti must be all, firms or inferred")


def _parse_bbox(bbox: str | None):
    if not bbox:
        return None
    try:
        v = tuple(float(x) for x in bbox.split(","))
        assert len(v) == 4
    except Exception:
        raise HTTPException(422, "bbox must be minLon,minLat,maxLon,maxLat")
    # Normalise longitudes (MapLibre may give values beyond +-180 after panning)
    x0, y0, x1, y1 = v
    if x1 - x0 >= 360:
        return (-180.0, max(y0, -90.0), 180.0, min(y1, 90.0))
    norm = lambda x: ((x + 180) % 360) - 180
    return (norm(x0), max(y0, -90.0), norm(x1) if norm(x1) != -180 else 180.0, min(y1, 90.0))


@router.get("/available-dates")
def available_dates():
    return _available_dates()


@lru_cache(maxsize=1)
def _available_dates():
    con = q.con()
    d = _daily_src()
    first, last = con.execute(f"SELECT min(date), max(date) FROM {d}").fetchone()
    sats = con.execute(f"SELECT satellite, min(date), max(date), sum(n) FROM {d} GROUP BY 1").fetchall()
    code = {v: k for k, v in SAT_CODES.items()}
    m = q.meta()
    return {
        "first": first.isoformat(), "last": last.isoformat(),
        "years": list(range(first.year, last.year + 1)),
        "satellites": [{"code": code.get(s, s), "label": SAT_LABELS.get(s, s), "first": a.isoformat(),
                        "last": b.isoformat(), "count": int(n)}
                       for s, a, b, n in sorted(sats, key=lambda r: SAT_INDEX.index(r[0]) if r[0] in SAT_INDEX else 9)],
        "regions": [],  # global data: no per-country regions
        "series_rule": {"viirs_start": config.SETTINGS.viirs_start, "aqua_start": publish.AQUA_START.isoformat()},
        "nrt_first": m["nrt_first"],
        "type_inferred_note": m["type_inferred_note"],
    }


@router.get("/hotspots")
def hotspots(
    date: str, window: str = "day", bbox: str | None = None,
    sensors: str = "terra,aqua,snpp,n20,n21", mode: str = "harmonized",
    types: str = "0,1,2,3", conf: str = "low,nominal,high", daynight: str = "D,N",
    region: str = "all", frp_min: float | None = None, frp_max: float | None = None,
    hour_min: int | None = Query(None, ge=0, le=23), hour_max: int | None = Query(None, ge=0, le=23),
    zoom: float = 2, ti: str = "all",
):
    if mode not in ("harmonized", "raw"):
        raise HTTPException(422, "mode must be harmonized or raw")
    _check_ti(ti)
    return _hotspots(date, window, _parse_bbox(bbox), _csv(sensors), mode, _csv(types), _csv(conf),
                     _csv(daynight), frp_min, frp_max, hour_min, hour_max, round(zoom), ti)


@lru_cache(maxsize=256)
def _hotspots(date, window, bbox, sensors, mode, types, conf, daynight, frp_min, frp_max,
              hour_min, hour_max, zoom, ti="all"):
    d0, d1 = _window(date, window)
    w, params = _filters(sensors, mode, types, conf, daynight, bbox, frp_min, frp_max, hour_min, hour_max, ti)
    pts = f"(SELECT * FROM {_points_src(d0, d1, bbox)} WHERE {' AND '.join(w)})"
    con = q.con()
    with q.time_limit(con, "The hotspots query"):
        return _hotspots_run(con, pts, params, date, window, d0, d1, mode, zoom)


def _hotspots_run(con, pts, params, date, window, d0, d1, mode, zoom):
    n, wsum, ninf = con.execute("SELECT count(*), coalesce(sum(weight), 0), count(*) FILTER (WHERE type_inferred) "
                                f"FROM {pts}", params).fetchone()
    base = {"date": date, "window": window, "from": d0.isoformat(), "to": d1.isoformat(), "mode": mode,
            "count": int(n), "weight_sum": round(float(wsum), 1), "type_inferred_count": int(ninf)}
    if n > MAX_POINTS:
        cell = 0.25 if zoom < 4 else 0.1 if zoom < 6 else 0.05
        rows = con.execute(f"""
            SELECT round((floor(lon / {cell}) + 0.5) * {cell}, 4), round((floor(lat / {cell}) + 0.5) * {cell}, 4),
                   count(*), round(sum(greatest(frp, 0)), 1), round(sum(weight), 2), round(max(frp), 1)
            FROM {pts} GROUP BY 1, 2""", params).fetchall()
        cols = list(zip(*rows)) if rows else [[]] * 6
        return {**base, "aggregated": True, "cell_deg": cell,
                "lon": list(cols[0]), "lat": list(cols[1]), "n": list(cols[2]), "frp_sum": list(cols[3]),
                "w": list(cols[4]), "frp_max": list(cols[5])}
    r = con.execute(f"""
        SELECT round(lon, 5) AS lon, round(lat, 5) AS lat, round(frp, 1) AS frp,
               strftime(datetime_utc, '%Y-%m-%d %H:%M') AS t,
               CASE satellite WHEN 'Terra' THEN 0 WHEN 'Aqua' THEN 1 WHEN 'N' THEN 2 WHEN 'N20' THEN 3 ELSE 4 END AS sat,
               type, CASE confidence_norm WHEN 'low' THEN 0 WHEN 'nominal' THEN 1 WHEN 'high' THEN 2 ELSE -1 END AS conf,
               CASE WHEN daynight = 'D' THEN 1 ELSE 0 END AS dn, round(CAST(weight AS DOUBLE), 3) AS w,
               round(CAST(coalesce(scan, 1.0) AS DOUBLE), 2) AS scan, round(CAST(coalesce(track, 1.0) AS DOUBLE), 2) AS track,
               CAST(in_calendar AS INTEGER) AS cal, CAST(type_inferred AS INTEGER) AS ti, version
        FROM {pts} ORDER BY frp""", params).fetchnumpy()
    return {**base, "aggregated": False, "sat_index": SAT_INDEX, "conf_index": CONF_INDEX,
            **{k: v.tolist() for k, v in r.items()}}


@router.get("/timeline-histogram")
def timeline_histogram(
    from_: str = Query(..., alias="from"), to: str = Query(...), granularity: str = "day",
    bbox: str | None = None, sensors: str = "terra,aqua,snpp,n20,n21", mode: str = "harmonized",
    types: str = "0,1,2,3", conf: str = "low,nominal,high", daynight: str = "D,N", region: str = "all",
    frp_min: float | None = None, frp_max: float | None = None,
    hour_min: int | None = Query(None, ge=0, le=23), hour_max: int | None = Query(None, ge=0, le=23),
    ti: str = "all",
):
    if granularity not in ("day", "month"):
        raise HTTPException(422, "granularity must be day or month")
    if mode not in ("harmonized", "raw"):
        raise HTTPException(422, "mode must be harmonized or raw")
    _check_ti(ti)
    return _histogram(from_, to, granularity, _parse_bbox(bbox), _csv(sensors), mode, _csv(types), _csv(conf),
                      _csv(daynight), frp_min, frp_max, hour_min, hour_max, ti)


@lru_cache(maxsize=256)
def _histogram(d0, d1, gran, bbox, sensors, mode, types, conf, daynight, frp_min, frp_max, hour_min, hour_max,
               ti="all"):
    try:
        a, b = dt.date.fromisoformat(d0), dt.date.fromisoformat(d1)
    except ValueError:
        raise HTTPException(422, "from/to must be YYYY-MM-DD")
    bucket = "date" if gran == "day" else "date_trunc('month', date)"
    con = q.con()
    detail = frp_min is not None or frp_max is not None or (hour_min or 0) > 0 \
        or (hour_max is not None and hour_max < 23)
    w, params = _filters(sensors, mode, types, conf, daynight, bbox, frp_min, frp_max, hour_min, hour_max, ti)
    note = None
    if bbox is not None and not detail:
        # Area timeline (step 10e, user decisions): precomputed 0.25 deg cell x month counts in the image instead of
        # scanning the detections on the bucket (could not finish on Cloud Run). Always monthly bars, also when days
        # are asked: the response says "month" and the web timeline draws the bars from the response.
        source, note, gran = "area_cells", AREA_MONTH_NOTE, "month"
        aw, aparams = _filters(sensors, mode, types, conf, daynight, None, None, None, None, None, ti)
        rows = _area_months(con, a, b, bbox, aw, aparams, mode)
    elif detail:
        # FRP / hour filters: live count from the detections, limited span (a longer one could not finish on Cloud Run).
        if (b - a).days > LIVE_MAX_DAYS:
            raise HTTPException(422, f"With FRP or hour filters the timeline covers at most {LIVE_MAX_DAYS} days: "
                                     "zoom the timeline in.")
        source = "detections"
        val = "sum(CASE WHEN weight > 0 THEN weight ELSE 1 END)" if mode == "harmonized" else "count(*)"
        sql = (f"SELECT {bucket} AS t, count(*) AS n, {val} AS v FROM {_points_src(a, b, bbox)} "
               f"WHERE {' AND '.join(w)} GROUP BY 1 ORDER BY 1")
        with q.time_limit(con, "The timeline query"):
            rows = con.execute(sql, params).fetchall()
    else:  # fast path: precomputed daily counts (no bbox / FRP / hour filters)
        source = "daily_counts"
        val = "sum(w)" if mode == "harmonized" else "sum(n)"
        sql = (f"SELECT {bucket} AS t, sum(n) AS n, {val} AS v FROM {_daily_src()} "
               f"WHERE date BETWEEN ? AND ? AND {' AND '.join(w)} GROUP BY 1 ORDER BY 1")
        rows = con.execute(sql, [a, b] + params).fetchall()
    out = {"granularity": gran, "mode": mode, "source": source,
           "unit": "VIIRS-equivalent detections" if mode == "harmonized" else "detections",
           "t": [r[0].strftime("%Y-%m-%d") for r in rows], "n": [int(r[1]) for r in rows],
           "v": [round(float(r[2]), 1) for r in rows]}
    return {**out, "note": note} if note else out


LIVE_MAX_DAYS = 450  # = the timeline's day / month switch: live month requests over an area are not served
AREA_MONTH_NOTE = ("Area timeline by month (also when zoomed in) from precomputed 0.25° cell counts: it covers the cells "
                   "whose centre lies in the area, so the edges follow the 0.25° grid. All filters apply except FRP "
                   "and hours.")


def _area_months(con, a: dt.date, b: dt.date, bbox, w: list[str], params: list, mode) -> list[tuple]:
    """(month, n, v) from area_month_g<grid>.parquet over the cells whose centre lies in `bbox` (antimeridian-aware).
    `w` / `params`: the _filters() clauses without area, FRP or hours (same columns as the daily counts table)."""
    p = publish.area_path()
    if not p.exists():
        raise HTTPException(503, f"{p.name} is missing: run `python -m pipeline.publish area`")
    h = config.SETTINGS.grid_deg / 2
    x0, y0, x1, y1 = bbox
    lon = (f"cell_lon + {h} BETWEEN {x0} AND {x1}" if x0 <= x1
           else f"(cell_lon + {h} >= {x0} OR cell_lon + {h} <= {x1})")
    val = "sum(w)" if mode == "harmonized" else "sum(n)"
    return con.execute(f"""
        SELECT month_start AS t, sum(n) AS n, {val} AS v FROM '{config.sql_path(p)}'
        WHERE month_start BETWEEN ? AND ? AND cell_lat + {h} BETWEEN {y0} AND {y1} AND {lon} AND {' AND '.join(w)}
        GROUP BY 1 ORDER BY 1""", [a.replace(day=1), b] + params).fetchall()
