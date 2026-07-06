# Crawler (VnExpress) → Iceberg Bronze

News crawler that writes articles **directly into the Iceberg Bronze table**
`bronze.vnexpress_articles` via the Iceberg REST Catalog (data lands in the
`warehouse` bucket on MinIO). Runs **inside its own container** — it does *not*
run on Trino. There is no separate `raw` zone.

```
newscrawler ──HTTP──▶ vnexpress.net
   └── PyIceberg append ──▶ iceberg.bronze.vnexpress_articles   (warehouse on MinIO)
```

Once written, the data is immediately queryable through Trino / any SQL tool:

```sql
SELECT * FROM iceberg.bronze.vnexpress_articles;
```

## Package layout

| Module | Role |
| ------ | ---- |
| [newscrawler/config.py](newscrawler/config.py)   | Base URL, category list, request headers/delays, `MAX_PAGES` |
| [newscrawler/fetcher.py](newscrawler/fetcher.py) | `requests` session with retry/backoff → `(html, status)` |
| [newscrawler/parser.py](newscrawler/parser.py)   | `Article` dataclass + VnExpress HTML parsing |
| [newscrawler/storage.py](newscrawler/storage.py) | Appends a batch to the Iceberg Bronze table via PyIceberg |
| [newscrawler/main.py](newscrawler/main.py)       | Orchestration: paginate categories, filter by date, save |

## Run

Behind a compose profile, so `docker compose up` does **not** start it. Run on
demand:

```bash
# crawl ALL categories for today (heavy!)
docker compose run --rm --no-deps crawler

# crawl only specific categories (names from config.py CATEGORIES)
docker compose run --rm --no-deps -e CRAWL_CATEGORIES="Kinh doanh,Thế giới" crawler

# crawl a specific target date
docker compose run --rm --no-deps -e CRAWL_DATE="2026-07-02" crawler
```

## Configuration

- **What to crawl / how politely:** edit [newscrawler/config.py](newscrawler/config.py)
  (`CATEGORIES`, `REQUEST_DELAY`, `MAX_PAGES`, …), then `docker compose build crawler`.
- **Catalog / storage connection:** environment variables (set in `docker-compose.yml`):

  | Env | Default | Meaning |
  | --- | --- | --- |
  | `ICEBERG_REST_URI` | `http://iceberg-rest:8181` | Iceberg REST Catalog |
  | `ICEBERG_WAREHOUSE` | `s3://warehouse/` | warehouse location |
  | `MINIO_ENDPOINT` | `http://minio:9000` | S3 endpoint |
  | `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | `minioadmin` | credentials |
  | `AWS_REGION` | `us-east-1` | region |
  | `BRONZE_NAMESPACE` / `BRONZE_TABLE` | `bronze` / `vnexpress_articles` | target table |
  | `CRAWL_CATEGORIES` | *(all)* | comma-separated category names |
  | `CRAWL_DATE` | *(today)* | ISO target date |

## Bronze table schema

`url_hash, url, article_id, category, sub_category, topic, tags[], title,
description, content, word_count, thumbnail_url, author_name, author_email,
source_attribution, published_at, updated_at, crawled_at, raw_date, source,
crawl_status, http_status`

The table is created on first run (namespace + table auto-created). Each crawl
run **appends** (one Iceberg snapshot per category). Bronze is append-only;
de-duplication / cleaning happens later in the Silver layer.
