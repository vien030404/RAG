# Crawler (đa nguồn) → raw HTML + Iceberg Bronze

Trình crawl tin tức **đa nguồn** (VnExpress, VietnamNet, Tuổi Trẻ, Thanh Niên,
Dân trí). Với mỗi bài nó làm 2 việc:

1. **Lưu HTML thô (nén gzip)** vào bucket `raw` trên MinIO — tầng raw, cho phép
   *re-parse* về sau mà không cần crawl lại.
2. **Ghi bản đã parse** vào bảng Iceberg **`bronze.articles`** (thống nhất mọi
   nguồn, phân biệt qua cột `source`) qua Iceberg REST Catalog (warehouse trên MinIO).

Chạy **trong container riêng** (one-shot job).

```
newscrawler ──HTTP──▶ 5 báo
   ├── gzip HTML  ──▶ s3://raw/source=<src>/dt=<YYYY-MM-DD>/<url_hash>.html.gz
   └── PyIceberg  ──▶ bronze.articles   (warehouse on MinIO)
```

Đọc dữ liệu bằng PyIceberg (không có engine SQL trong stack):

```python
cat.load_table("bronze.articles").scan().to_pandas()
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
| [newscrawler/compact.py](newscrawler/compact.py)   | Dọn `bronze.articles` tại chỗ — chạy tay khi cần sửa hồi tố |
| [newscrawler/chunker.py](newscrawler/chunker.py)   | Cắt bài thành chunk (theo đoạn văn, mọi chunk gắn title+sapo) |
| [newscrawler/bm25.py](newscrawler/bm25.py)         | Sparse vector BM25 tiếng Việt (unigram + bigram) cho hybrid search |
| [newscrawler/vectorstore.py](newscrawler/vectorstore.py) | Qdrant: collection hybrid, payload index, RRF fusion |
| [newscrawler/gemini.py](newscrawler/gemini.py)     | Bể API key Gemini — xoay vòng khi 1 key hết quota |
| [newscrawler/embed.py](newscrawler/embed.py)       | Bronze → chunk → embedding Gemini → Qdrant (tăng dần) |
| [newscrawler/ask.py](newscrawler/ask.py)           | Hỏi-đáp: truy vấn → Qdrant → Gemini trả lời kèm nguồn |
| [newscrawler/peek.py](newscrawler/peek.py)         | Xem bảng Bronze dạng bảng, thống kê, xuất CSV |

## Nguồn & parser

| source | CMS / ghi chú parse |
| ------ | ------------------- |
| `vnexpress`  | selector ổn định (`h1.title-detail`, `article.fck_detail`, meta pubdate) |
| `vietnamnet` | `h1.content-detail-title`; published parse text `div.publish-date` (không có meta ISO) |
| `tuoitre` + `thanhnien` | **chung 1 parser** `AfcbcSource` (`[data-role=content].afcbc-body`, meta `article:published_time` ISO); phân trang qua endpoint `/timeline/{zone}/…` với `zone` lấy từ `<input#hdZoneId>` |
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
  tuoitre `/timeline/{zone}/trang-{n}.htm`, thanhnien `/timeline/{zone}/{n}.htm`
  (`{zone}` = `<input#hdZoneId>` trên trang category; trang 1 chỉ quét khối
  `data-marked-zoneid=*_cate_stream` để bỏ link sidebar cũ).
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
source_attribution[], published_at, crawled_at, source, crawl_status,
published_date, content_len`

`tags[]` và `source_attribution[]` là **mảng**: một bài thường có nhiều từ khóa
và dẫn lại từ nhiều hãng ("AFP, Reuters"). Để nguyên chuỗi thì lọc
`= 'Reuters'` trượt hết những bài ghi kèm hãng khác — đo được 430/627 dòng có
nhiều hãng, sinh 452 chuỗi khác nhau cho 350 hãng thật.

Bảng tự tạo ở lần chạy đầu. Mỗi nguồn → 1 `append` (1 snapshot). Bronze
**append-only**: chạy lại cùng một ngày sẽ sinh thêm bản của cùng bài — cố ý,
Crawl là idempotent: trước khi fetch, `storage.known_published_dates()` đọc
`url_hash` đã có của nguồn nên chạy lại cùng một ngày không sinh dòng trùng
và cũng không tốn băng thông fetch lại.

## Dọn bảng (chạy tay)

```bash
docker compose run --rm --no-deps crawler -m newscrawler.compact
```

`compact` khử trùng + áp lại luật lọc + chuẩn hóa `category` + tính lại
`published_date`, `content_len` cho TOÀN bảng, ghi bằng `overwrite`. Bình thường
không cần chạy — crawl đã idempotent và cột dẫn xuất được tính ngay lúc ghi.
Dùng khi sửa hồi tố (vá parser rồi parse lại HTML thô, đổi luật lọc cho dữ liệu
cũ, **đổi kiểu cột** — Iceberg không cho `ALTER TYPE` nên compact xóa rồi thêm
lại cột và ghi đè toàn bộ dữ liệu, xem `source_attribution`).

**Chỉ chạy khi không có crawl nào đang chạy** — Bronze có 4 task `append` song
song, `overwrite` cùng lúc sẽ tranh chấp commit.

- **Dedup**: khóa `(source, article_id)` (fallback `url_hash`), giữ bản
  `crawled_at` mới nhất — bài có thể được báo cập nhật.
- **Chuẩn hóa category** chéo nguồn: `xa_hoi`→`thoi_su` (dantri),
  `kinh_te`→`kinh_doanh` (thanhnien), `nhip_song_so`/`khoa_hoc`→`cong_nghe`
  (tuoitre/vnexpress). Không có bước này thì lọc theo category sẽ sót nguồn.
- **Lọc**: bỏ dòng `crawl_status != success`, `published_at` null, content quá ngắn.
- **Idempotent**: `overwrite` toàn bảng → chạy lại bao nhiêu lần cũng cùng kết quả,
  và sửa rule rồi chạy lại là áp dụng cho toàn bộ lịch sử, không cần crawl lại.

## Xem dữ liệu

### SQL — Trino

Trino đọc Iceberg qua REST Catalog nên luôn thấy **đúng snapshot hiện hành**.
Chạy như một service (`docker compose up -d trino`), nghe ở `localhost:8080`.

```bash
# CLI tương tác
docker exec -it trino trino --catalog iceberg --schema bronze

# Một câu rồi thoát
docker exec trino trino --catalog iceberg --schema bronze --output-format ALIGNED \
  --execute "SELECT source, count(*) AS bai FROM articles GROUP BY 1 ORDER BY 2 DESC"
```

`--output-format` nhận `ALIGNED` (bảng kẻ), `CSV`, `JSON`, `TSV`, `VERTICAL`.

Cắm SQL client bên ngoài: `jdbc:trino://localhost:8080`, user bất kỳ (ví dụ
`admin`), **không** mật khẩu. Driver Trino có sẵn trong DBeaver/DataGrip.

### Xem nhanh / xuất CSV — [peek.py](newscrawler/peek.py)

```bash
docker compose run --rm crawler -m newscrawler.peek                 # 10 bài mới nhất
docker compose run --rm crawler -m newscrawler.peek --stats         # thống kê nguồn/chuyên mục/ngày
docker compose run --rm crawler -m newscrawler.peek --schema        # 21 cột + kiểu
docker compose run --rm crawler -m newscrawler.peek --table bronze.articles

# lọc + chọn cột
docker compose run --rm crawler -m newscrawler.peek -n 30 \
    --source tuoitre --date 2026-08-26 --cols published_at,title,author_name,tags

# đọc trọn 1 bài (kèm metadata + toàn văn)
docker compose run --rm crawler -m newscrawler.peek --id 10026082617134521

# xuất CSV để mở bằng Excel (mount thư mục ra ngoài)
docker compose run --rm -v "$PWD/out:/out" crawler -m newscrawler.peek \
    --csv /out/tin.csv --limit 0 --cols published_at,source,category,title,url
```

> **Git Bash trên Windows**: thêm `MSYS_NO_PATHCONV=1` trước lệnh, nếu không `/out/tin.csv`
> bị đổi thành đường dẫn Windows và ghi hụt.

Cột mảng (`tags`) được nối bằng `;` khi xuất CSV. **Không đọc thẳng Parquet trên
MinIO** — `compact` ghi bằng `overwrite` nên file của snapshot cũ vẫn còn, đọc thô
sẽ ra dữ liệu trùng/cũ; phải đi qua PyIceberg để giải đúng snapshot hiện hành.

## RAG (chunk → embedding → Qdrant → hỏi-đáp)

Cần `GEMINI_API_KEY` trong `.env` ([aistudio.google.com/apikey](https://aistudio.google.com/apikey)).

```bash
docker compose up -d qdrant                        # vector store (dashboard :6333/dashboard)
docker compose run --rm crawler -m newscrawler.embed          # embed bài chưa có
docker compose run --rm crawler -m newscrawler.ask "Giá vàng hôm qua thế nào?"

# lọc theo nguồn / chuyên mục / khoảng ngày
docker compose run --rm crawler -m newscrawler.ask --source vnexpress --days 3 "Tin thể thao nổi bật?"
```

### Cột nào đi đâu

21 cột của `bronze.articles` được dùng ở 3 chỗ khác nhau:

Ba vai trò khác nhau, **không trộn lẫn**:

| cột | dense (embed) | BM25 sparse | payload |
| --- | :---: | :---: | --- |
| `title`, `description`, `content` | ✅ | ✅ | ✅ |
| `tags[]` (từ khóa chủ đề) | ❌ | ✅ | ✅ **index** (khớp phần tử) |
| `source_attribution[]` | ❌ | ❌ | ✅ **index** (khớp phần tử) |
| `author_name`, `sub_category`, `topic` | ❌ | ❌ | ✅ **index** (lọc chính xác) |
| `source`, `category`, `published_ts`, `published_date`, `article_id` | ❌ | ❌ | ✅ **index** |
| `url`, `thumbnail_url`, `published_at`, `word_count` | ❌ | ❌ | ✅ hiển thị |
| `url_hash`, `crawled_at`, `content_len`, `crawl_status` | ❌ | ❌ | ❌ thừa |

**Nguyên tắc: trường ĐỊNH DANH không được trộn vào text.** Nhét `author_name`
vào chunk thì "Ngọc Ánh" *tác giả* lẫn với "Ngọc Ánh" *được nhắc trong bài* —
mất tính định vị, và câu hỏi "bài của Ngọc Ánh" sẽ trả cả bài **viết về** người
đó. Chúng nằm ở payload có index, lọc bằng so khớp chính xác:
`--author`, `--tag`, `--attribution`, `--source`, `--category`, `--days`.

`sub_category` **không duy nhất toàn cục** — "Tin tức" xuất hiện ở 3 category
của 3 nguồn khác nhau. Lọc nó một mình sẽ gom lẫn; phải đi kèm `category`
(và thường cả `source`).

Riêng `tags` thì hòa vào BM25 được, vì đó là **từ khóa chủ đề** do tòa soạn gán,
cùng bản chất với nội dung — và là loại danh từ riêng dense vector hay trượt.

**Bigram không bắc qua ranh giới dòng** — tiêu đề / sapo / từng đoạn / từng tag
là các đoạn độc lập. Nếu ghép qua ranh giới sẽ sinh term rác:

```
tags ['Tổng thống Iran','Mỹ','Iran','Tin nóng'] nối bằng dấu cách
  -> iran_mỹ, mỹ_iran, iran_tin, thang_tổng   ← rác, vẫn được tính điểm
mỗi tag một dòng
  -> tổng_thống, thống_iran, tin_nóng          ← chỉ còn bigram thật
```

**Chunk** ([chunker.py](newscrawler/chunker.py)): cắt theo ranh giới đoạn văn, không
bao giờ cắt giữa đoạn; bài ≤ 800 token giữ nguyên 1 chunk; overlap 1 đoạn. **Mọi
chunk đều gắn `title` + `description` ở đầu** — đoạn giữa bài hay dùng đại từ
("ông", "công ty này"), thiếu tiêu đề thì retrieval trượt. Đo trên 2733 bài:
~2 chunk/bài, p50 ≈ 705 token.

**Embed** ([embed.py](newscrawler/embed.py)): `gemini-embedding-001`, 768 chiều
(Matryoshka — nhẹ hơn 4 lần bản 3072, phải tự chuẩn hóa vector sau khi cắt chiều).
Point id = `uuid5(source, article_id, chunk_idx)` nên ổn định giữa các lần chạy →
chỉ embed bài **mới** hoặc bài có `content_hash` **đổi**. `EMBED_FORCE=1` để embed
lại tất cả (khi đổi model/số chiều).

**Hybrid search** ([bm25.py](newscrawler/bm25.py) + [vectorstore.py](newscrawler/vectorstore.py)):
collection có 2 vector đặt tên — `dense` (Gemini, cosine) bắt **ngữ nghĩa**, và
`bm25` (sparse) bắt **khớp chính xác từ**: tên riêng, số liệu, mã hiệu — vốn là
điểm yếu của dense vector. Truy vấn chạy song song 2 nhánh rồi hợp nhất bằng
**RRF** ngay trên server (1 request). RRF chỉ dùng *thứ hạng* nên không phải
chuẩn hóa điểm giữa cosine và BM25 — hai thang vốn không so sánh trực tiếp được.

- IDF do Qdrant tính (`Modifier.IDF`) → client chỉ gửi term frequency, và IDF
  luôn đúng khi kho tin lớn dần.
- Tiếng Việt viết rời từng âm tiết nên ngoài unigram còn sinh **bigram**
  ("giá_vàng") — dạng từ phổ biến nhất trong tiếng Việt. Tắt bằng `BM25_BIGRAM=0`.
- `BM25_AVGDL=645` đo trên 5416 chunk thật; đo lại nếu đổi `CHUNK_MAX_TOKENS`.

**Hỏi-đáp** ([ask.py](newscrawler/ask.py)): truy vấn embed với
`task_type=RETRIEVAL_QUERY` (khác `RETRIEVAL_DOCUMENT` lúc index — dùng sai giảm
rõ chất lượng), lọc metadata trong Qdrant, gộp chunk cùng bài, rồi để Gemini trả
lời **chỉ dựa trên đoạn trích**, có trích dẫn `[1] [2]`; không đủ căn cứ thì nói
không tìm thấy.

## Raw zone

Object HTML nén tại `s3://raw/source=<src>/dt=<YYYY-MM-DD>/<url_hash>.html.gz`
(gzip, `Content-Encoding: gzip`). Đây là object thường (không phải Iceberg),
là "Bronze thật" theo nghĩa giữ nguyên nguồn — dùng để re-parse khi parser đổi.
