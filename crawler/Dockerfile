# ============================================================================
# Multi-source Vietnamese news crawler (vnexpress, vietnamnet, tuoitre,
# thanhnien, dantri). Saves raw gzipped HTML to the `raw` bucket, then writes
# parsed rows to Iceberg Bronze (`bronze.articles`). One-shot job, invoked
# manually (`docker compose run --rm crawler`) or by Airflow.
# ============================================================================
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY newscrawler/ ./newscrawler/
COPY chat_app.py .

# Run as an unprivileged user.
RUN useradd --create-home --uid 1000 crawler && chown -R crawler:crawler /app
USER crawler

# Scope is controlled via env: CRAWL_CATEGORIES (comma-separated names),
# CRAWL_DATE (ISO date). Defaults to all categories, yesterday (VN time).
#
# ENTRYPOINT/CMD tách đôi để chạy được module khác cùng image, ví dụ
# Bronze -> Silver:  docker run ... crawler:0.1 -m newscrawler.silver
ENTRYPOINT ["python"]
CMD ["-m", "newscrawler.main"]
