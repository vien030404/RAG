# Crawler (đa nguồn) → raw HTML + Iceberg Bronze

Trình crawl tin tức **đa nguồn** (VnExpress, VietnamNet, Tuổi Trẻ, Thanh Niên,
Dân trí). Với mỗi bài nó làm 2 việc:

1. **Lưu HTML thô (nén gzip)** vào bucket `raw` trên MinIO — tầng raw, cho phép
   *re-parse* về sau mà không cần crawl lại.
2. **Ghi bản đã parse** vào bảng Iceberg **`bronze.articles`** (thống nhất mọi
   nguồn, phân biệt qua cột `source`) qua Iceberg REST Catalog (warehouse trên MinIO).

Chạy **trong container riêng** — không chạy trên Trino.

```
newscrawler ──HTTP──▶ 5 báo
   ├── gzip HTML  ──▶ s3://raw/source=<src>/dt=<YYYY-MM-DD>/<url_hash>.html.gz
   └── PyIceberg  ──▶ iceberg.bronze.articles   (warehouse on MinIO)
```

Truy vấn ngay qua Trino:

```sql
SELECT source, count(*) FROM iceberg.bronze.articles GROUP BY source;
```

## Package layout

| Module | Role |
| ------ | ---- |
| [newscrawler/config.py](newscrawler/config.py)     | Headers, delays, `MAX_PAGES`, `MAX_ARTICLES_PER_CATEGORY` |
| [newscrawler/fetcher.py](newscrawler/fetcher.py)   | `requests` session retry/backoff → `(html, status)` |
| [newscrawler/article.py](newscrawler/article.py)   | `Article` dataclass (schema chung mọi nguồn) |
| [newscrawler/sources/](newscrawler/sources/)       | `BaseSource` + 1 parser / báo + registry `SOURCES` |
| [newscrawler/raw_storage.py](newscrawler/raw_storage.py) | gzip HTML → bucket `raw` (boto3) |
| [newscrawler/storage.py](newscrawler/storage.py)   | Append batch vào `bronze.articles` (PyIceberg) |
| [newscrawler/main.py](newscrawler/main.py)         | Orchestration: nguồn × category → fetch → raw → parse → bronze |

## Nguồn & parser

| source | CMS / ghi chú parse |
| ------ | ------------------- |
| `vnexpress`  | selector ổn định (`h1.title-detail`, `article.fck_detail`, meta pubdate) |
| `vietnamnet` | `h1.content-detail-title`; published parse text `div.publish-date` (không có meta ISO) |
| `tuoitre` + `thanhnien` | **chung 1 parser** `AfcbcSource` (`[data-role=content].afcbc-body`, meta `article:published_time` ISO) |
| `dantri`     | Tailwind (class không ổn định) → dựa meta + `<time>` + fallback mật độ `<p>`; 2 layout thường/e-magazine |

Thêm báo mới = thêm 1 file trong `sources/` + 1 dòng vào `sources/__init__.py`.

## Run

Sau compose profile `crawler`, nên `docker compose up` **không** khởi động nó. Chạy on-demand:

```bash
# crawl TẤT CẢ nguồn, tất cả category (nặng)
docker compose run --rm crawler

# giới hạn nguồn
docker compose run --rm -e CRAWL_SOURCES="vnexpress,tuoitre" crawler

# giới hạn category (tên áp cho mọi nguồn được chọn)
docker compose run --rm -e CRAWL_CATEGORIES="Thời sự,Thế giới" crawler

# crawl đúng 1 ngày (lọc theo published_at)
docker compose run --rm -e CRAWL_DATE="2026-07-06" crawler
```

## Cấu hình

- **Phạm vi / lịch sự:** sửa [newscrawler/config.py](newscrawler/config.py)
  (`MAX_PAGES` = số trang listing/category, `MIN_CONTENT_LEN`, `REQUEST_DELAY`);
  category từng báo nằm trong lớp source. Sửa xong `docker compose build crawler`.
  Phân trang: vnexpress `-p{n}`, vietnamnet `-page{n}`, dantri `/trang-{n}.htm`;
  **tuoitre/thanhnien chỉ trang 1** (load-more AJAX, zoneId không trích ổn định).
- **Kết nối catalog / storage** (env trong `docker-compose.yml`):

  | Env | Default | Ý nghĩa |
  | --- | --- | --- |
  | `ICEBERG_REST_URI` | `http://iceberg-rest:8181` | Iceberg REST Catalog |
  | `ICEBERG_WAREHOUSE` | `s3://warehouse/` | warehouse |
  | `RAW_BUCKET` | `raw` | bucket lưu HTML nén |
  | `MINIO_ENDPOINT` | `http://minio:9000` | S3 endpoint |
  | `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | `minioadmin` | credentials |
  | `BRONZE_NAMESPACE` / `BRONZE_TABLE` | `bronze` / `articles` | bảng đích |
  | `CRAWL_SOURCES` | *(tất cả)* | tên nguồn, phẩy ngăn cách |
  | `CRAWL_CATEGORIES` | *(tất cả)* | tên category, phẩy ngăn cách |
  | `CRAWL_DATE` | *(không lọc)* | ngày ISO cần crawl |

## Bronze table schema

`url_hash, url, article_id, category, sub_category, topic, tags[], title,
description, content, word_count, thumbnail_url, author_name,
source_attribution, published_at, crawled_at, source, crawl_status`

Bảng tự tạo ở lần chạy đầu. Mỗi category → 1 `append` (1 snapshot). Bronze
append-only; khử trùng lặp / chuẩn hóa chéo nguồn để dành tầng Silver (dbt).

## Raw zone

Object HTML nén tại `s3://raw/source=<src>/dt=<YYYY-MM-DD>/<url_hash>.html.gz`
(gzip, `Content-Encoding: gzip`). Đây là object thường (không phải Iceberg),
là "Bronze thật" theo nghĩa giữ nguyên nguồn — dùng để re-parse khi parser đổi.
