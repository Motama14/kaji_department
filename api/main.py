# SI-assisted: written with Claude Code (Anthropic LLM) under the author's direction and reviewed by the author.
"""FastAPI app: harmonized burn calendar, anomalies, CSV export and the world hotspot viewer (global data).

Run:  .venv/Scripts/python -m uvicorn api.main:app --port 8000
Docs: http://localhost:8000/docs

Environment (all optional): CORS_ORIGINS (comma-separated, default http://localhost:4200), CSV_MAX_ROWS
(default 1,000,000), plus the pipeline variables of pipeline/config.py (FIRE_PROCESSED_DIR, DUCKDB_*).
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from pipeline import config, publish

from . import queries as q
from . import viewer

app = FastAPI(title="Harmonized MODIS/VIIRS burn calendar", version="0.2.0")
app.add_middleware(CORSMiddleware,
                   allow_origins=[o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:4200").split(",")
                                  if o.strip()],
                   allow_methods=["*"], allow_headers=["*"])
app.add_middleware(GZipMiddleware, minimum_size=2048)
app.include_router(viewer.router)


class Point(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    radius_km: float = Field(25.0, gt=0, le=500)


class Area(BaseModel):
    """Exactly one of geometry (GeoJSON Polygon/MultiPolygon/Feature), bbox or point."""
    geometry: dict | None = None
    bbox: list[float] | None = Field(None, min_length=4, max_length=4,
                                     description="[min_lon, min_lat, max_lon, max_lat]")
    point: Point | None = None

    def geojson(self) -> str:
        try:
            return q.geometry_geojson(self.geometry, self.bbox, self.point.model_dump() if self.point else None)
        except (ValueError, KeyError, TypeError) as e:
            raise HTTPException(422, f"invalid area: {e}")


class CalendarRequest(Area):
    start: str | None = Field(None, description="YYYY-MM or YYYY-MM-DD")
    end: str | None = None


class AnomalyRequest(Area):
    year: int
    month: int = Field(ge=1, le=12)
    baseline_start: int | None = None
    baseline_end: int | None = None


class CsvRequest(Area):
    start: str | None = Field(None, description="YYYY-MM-DD")
    end: str | None = None
    only_calendar: bool = Field(False, description="Only detections used in the calendar")


@app.get("/api/health")
def health():
    return {"status": "ok", "harmonized": config.harmonized_path().exists(),
            "daily_counts": publish.daily_path().exists()}


@app.get("/api/meta")
def meta():
    """Data period, last data date, partial month and months whose detection type was inferred."""
    return {**q.meta(), "csv_max_rows": q.CSV_MAX_ROWS}


@app.post("/api/calendar")
def calendar(req: CalendarRequest):
    try:
        return q.calendar(req.geojson(), req.start, req.end)
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.post("/api/anomaly")
def anomaly(req: AnomalyRequest):
    base = (req.baseline_start, req.baseline_end) if req.baseline_start and req.baseline_end else None
    try:
        return q.anomaly(req.geojson(), req.year, req.month, base)
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.post("/api/hotspots.csv")
def hotspots_csv(req: CsvRequest):
    try:
        csv = q.hotspots_csv(req.geojson(), req.start, req.end, req.only_calendar)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except q.TooManyRows as e:
        raise HTTPException(413, str(e))
    return Response(csv, media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="hotspots.csv"'})


# Serve the built Angular app if present (single process for the local demo).
_web = Path(__file__).resolve().parents[1] / "web" / "dist" / "web" / "browser"
if _web.exists():
    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        """Serve the Angular build; unknown non-API paths fall back to index.html (client routing)."""
        if path.startswith("api/"):
            raise HTTPException(404)
        f = (_web / path).resolve()
        if path and f.is_file() and _web.resolve() in f.parents:
            return FileResponse(f)
        return FileResponse(_web / "index.html")
