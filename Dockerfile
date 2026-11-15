# SI-assisted: written with Claude Code (Anthropic LLM) under the author's direction and reviewed by the author.
# API image for Cloud Run: code + the small serving tables (about 270 MB). The 10.8 GB detections table is NOT in
# the image: it stays on the bucket, mounted read-only at /data (FIRE_HOTSPOTS_DIR=/data/hotspots, set at deploy).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FIRE_PROCESSED_DIR=/app/data/processed \
    FIRE_HOTSPOTS_DIR=/data/hotspots \
    FIRE_TMP_DIR=/tmp/duckdb \
    DUCKDB_MEMORY_LIMIT=2GB \
    DUCKDB_THREADS=2

WORKDIR /app

COPY requirements-api.txt .
RUN pip install --no-cache-dir -r requirements-api.txt

RUN python -c "import duckdb; duckdb.connect().execute('INSTALL spatial')"

# Small serving tables, straight from data/processed/ in the build context (no second copy on disk). Before the
# code so that code-only changes reuse this layer when the build cache is available.
# serving_facts.json lets the API start without scanning the detections (python -m pipeline.publish facts).
COPY data/processed/harmonized_g0.25.parquet \
     data/processed/daily_counts.parquet \
     data/processed/static_mask_d0.01.parquet \
     data/processed/coverage_sat_month.parquet \
     data/processed/serving_facts.json \
     /app/data/processed/

COPY pipeline/ pipeline/
COPY api/ api/

EXPOSE 8080
CMD exec uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8080}
