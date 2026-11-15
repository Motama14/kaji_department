FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FIRE_PROCESSED_DIR=/data \
    FIRE_TMP_DIR=/tmp/duckdb \
    DUCKDB_MEMORY_LIMIT=2GB \
    DUCKDB_THREADS=2

WORKDIR /app

COPY requirements-api.txt .
RUN pip install --no-cache-dir -r requirements-api.txt

RUN python -c "import duckdb; duckdb.connect().execute('INSTALL spatial')"

COPY pipeline/ pipeline/
COPY api/ api/

EXPOSE 8080
CMD exec uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8080}
