# ============================================================================
# VnExpress news crawler for the lakehouse `raw` zone.
# Runs as a one-shot job (crawl categories -> write Parquet to MinIO),
# invoked manually (`docker compose run --rm crawler`) or by Airflow later.
# ============================================================================
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY newscrawler/ ./newscrawler/

# Run as an unprivileged user.
RUN useradd --create-home --uid 1000 crawler && chown -R crawler:crawler /app
USER crawler

# Scope is controlled via env: CRAWL_CATEGORIES (comma-separated names),
# CRAWL_DATE (ISO date). Defaults to all categories, today.
ENTRYPOINT ["python", "-m", "newscrawler.main"]
