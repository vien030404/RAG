# News Crawler → RAG

Cào báo điện tử Việt Nam vào bảng Iceberg trên MinIO, chunk + embed sang Qdrant,
rồi hỏi-đáp bằng tiếng Việt qua giao diện Streamlit. Chạy bằng Docker Compose.

```
5 báo điện tử
     │  fetch (1 lần/bài, giãn nhịp 1s)
     ▼
 RAW      s3://raw/source=<báo>/dt=<ngày cào>/<url_hash>.html.gz
          HTML gốc nén gzip — nguồn để parse lại khi vá parser
     │  parse
     ▼
 BRONZE   iceberg.bronze.articles     1 dòng/bài · 20 cột · Parquet+zstd
     │  chunk → embed (Gemini) + BM25 sparse
     ▼
 QDRANT   news_chunks                 dense 768d + sparse bm25 + 11 payload index
     │  hybrid search → RRF → 3 cổng chống bịa
     ▼
 Streamlit · CLI           Airflow: crawl >> embed, 02:00 mỗi ngày
```

## Bắt đầu nhanh

```bash
cp .env.example .env            # điền GEMINI_API_KEYS
docker compose up -d --build

docker compose run --rm crawler -m newscrawler.main             # cào
docker compose run --rm --no-deps crawler -m newscrawler.embed  # embed
docker compose --profile chat up -d chatbot                     # localhost:8501
```

**Yêu cầu:** Docker Desktop (Engine 24+), ~4 GB RAM, và ít nhất một
[Gemini API key](https://aistudio.google.com/apikey).

> Hạn ngạch Gemini tính theo **project**, không theo key — nhiều key cùng một
> project dùng chung hạn mức. Muốn cộng dồn thì mỗi key một project riêng.

## Thành phần

| Service | Vai trò | Cổng |
|---|---|---|
| `postgres` | Metadata: catalog Iceberg + Airflow (2 database riêng) | `5432` |
| `minio` | Object store S3: bucket `warehouse` + `raw` | `9000` · `9001` console |
| `iceberg-rest` | Iceberg REST Catalog | `8181` |
| `trino` | SQL trên Iceberg — cắm DBeaver qua JDBC | `8080` |
| `qdrant` | Vector store (dense + sparse) | `6333` · `6334` gRPC |
| `airflow-scheduler` / `-webserver` | DAG `news_crawl` @ 02:00 | `8085` |
| `crawler` | Job một lần — profile `crawler` | — |
| `chatbot` | Giao diện Streamlit — profile `chat` | `8501` |

`make urls` in ra toàn bộ đường dẫn.

---

# Luồng dữ liệu

Ba tầng chạy độc lập, chỉ gặp nhau qua dữ liệu — không gọi hàm của nhau.

## Tầng 1 — Cào

`newscrawler/main.py`, vòng lặp **mỗi nguồn × mỗi chuyên mục**.

```
mỗi NGUỒN
 ├─ known = known_published_dates(source)      đọc Bronze 1 lần
 └─ mỗi CHUYÊN MỤC → crawl_category()
     ├─ trang 1 → parse_links() → danh sách URL
     ├─ more_page_urls() → trang 2..MAX_PAGES (20)
     └─ mỗi URL: bỏ qua nếu đã có → fetch → parse → lọc → giữ
 └─ save_articles(batch)                       1 append / nguồn
```

**Chống trùng trước khi fetch.** `known_published_dates()` đọc `url_hash` đã có
trong Bronze. Container mới, DAG retry hay trigger tay đều không cào lại bài cũ.
Trả kèm ngày xuất bản vì logic dừng phân trang cần biết ngày của cả link bị bỏ qua.

**Fetch một lần.** HTML parse ngay trong RAM rồi lưu lại nguyên văn:

```python
art_html, _ = fetch(link)                            # fetch duy nhất 1 lần
art = source.parse_article(art_html, link)
raw_storage.save_raw_html(src, md5(link), art_html)  # sau khi qua hết bộ lọc
```

**Dừng phân trang.** Listing xếp mới → cũ nhưng hay lẫn bài "nổi bật" ngày cũ, nên
không dừng vì một bài lẻ — chỉ dừng khi **cả trang** không còn bài nào mới bằng
`target_date`. Trần cứng 20 trang, chạm trần thì cảnh báo có thể thiếu.

**Bốn bộ lọc:**

| bỏ khi | vì sao |
|---|---|
| `crawl_status != "success"` | bài video / megastory / e-magazine — không có text để chunk |
| `len(content) < 100` | quá ngắn |
| `published_at is None` | không rõ ngày thì không xếp được |
| `published_date != target_date` | ngoài ngày đích |

**Chuẩn hoá nhãn lúc parse.** `demote_agency_author()` tách `author_name` (nhà báo)
khỏi `source_attribution` (hãng dẫn lại: TTXVN, PV, Reuters…) — trộn lẫn thì cả hai
bộ lọc đều sai. `fill_hierarchy()` đọc `BreadcrumbList` trong JSON-LD →
`sub_category` cấp 2, `topic` cấp 3.

**Ghi Bronze.** Khử trùng trong lô theo `article_id`; tính luôn cột dẫn xuất
(`published_date`, `content_len`); retry commit 5 lần vì 4 task nguồn append song
song vào cùng bảng.

> `dt=` trong đường dẫn raw là **ngày cào**, không phải ngày đăng. Xoá theo
> `published_date` sẽ không khớp prefix MinIO — phải dựng key từ `crawled_at`.

Raw zone không phải bản sao dự phòng — nó là nguồn để **parse lại** khi vá parser,
không cần chạm tới máy chủ báo.

### Nguồn

`vnexpress` · `vietnamnet` · `tuoitre` · `thanhnien`. Mỗi báo một lớp con
`BaseSource`, chỉ cần cài `parse_links` + `parse_article`; phần chung nằm ở
`sources/base.py`.

`dantri` đã gỡ khỏi DAG: trang bài có bot protection, request đầu 200 rồi HTTP 428
tức thì kể cả khi giãn 3 s. Dữ liệu cũ vẫn còn trong Bronze.

`category` lưu **đúng tên chuyên mục của báo đó** ("Nhịp sống số" của Tuổi Trẻ,
"Khoa học công nghệ" của VnExpress) — không chuẩn hoá chéo nguồn, vì cùng một tên
chưa chắc cùng nội dung. Lọc một tên sẽ bỏ sót bài tương đương ở báo khác.

## Tầng 2 — Index

`newscrawler/embed.py`. Đọc toàn bộ Bronze mỗi lần chạy, nhưng chỉ gọi API cho
phần thật sự đổi.

### Cắt chunk

```
chunk 0     summary   title + description       luôn có, 1 chunk/bài
chunk 1..n  body      content
            ├─ ≤ 1.400 token  →  1 chunk trọn vẹn
            └─ dài hơn        →  ceil(token/1200) phần BẰNG NHAU
                                 + overlap 1 đoạn giữa hai phần liền kề
```

Ranh giới cắt **luôn là ranh giới đoạn văn**, không bao giờ cắt giữa đoạn.

Chia đều thay vì nhồi đầy: nhồi 1.200 rồi để phần cuối cụt sẽ sinh chunk 1.200
token cạnh chunk 150 token, mà chunk cụt gần như không bao giờ thắng retrieval.

Chunk summary tồn tại vì câu hỏi tổng quát ("có tin gì về X?") khớp với **ý chính**
của bài chứ không khớp đoạn văn nào. Body chunk **không** dán title/description vào
đầu — nhân bản vào mọi chunk vừa làm loãng vector vừa khiến chúng giống nhau.

Thực tế: ~2,2 chunk/bài, body p50 ~880 token.

### Mỗi chunk là một point với ba cấu trúc song song

```
dense  768d   ← Gemini embed(text), chuẩn hoá L2
sparse        ← bm25.doc_vector(text + tags)
payload       ← 22 khoá, 11 có index
```

Payload dựng **một lần cho mỗi bài** rồi gán y nguyên cho mọi chunk; chỉ 4 khoá
khác nhau (`chunk_idx`, `n_chunks`, `chunk_type`, `text`). Nhờ vậy bộ lọc cho kết
quả như nhau bất kể trúng chunk nào — vector DB không join được sang bảng khác nên
metadata phải denormalize xuống từng chunk.

Point id = `uuid5(NS, "source:article_id:chunk_idx")` — suy ra được, không lưu ở
đâu. Chạy lại ghi đè đúng point cũ, không tạo bản trùng.

| trường | dense | BM25 | payload |
|---|:--:|:--:|:--:|
| `title`, `description` (summary) | ✅ | ✅ | hiển thị |
| `content` (body) | ✅ | ✅ | — |
| `tags[]` | ❌ | ✅ | ✅ index |
| `category`, `sub_category`, `topic` | ❌ | ❌ | ✅ index |
| `author_name`, `source_attribution[]` | ❌ | ❌ | ✅ index |
| `published_ts`/`_date`, `source`, `article_id`, `chunk_type` | ❌ | ❌ | ✅ index |
| `url`, `thumbnail_url`, `word_count`, `text` | ❌ | ❌ | hiển thị |

**Trường định danh không trộn vào text.** Nhét `author_name` vào chunk thì "Ngọc
Ánh" *tác giả* lẫn với "Ngọc Ánh" *được nhắc trong bài*, và câu hỏi "bài của Ngọc
Ánh" sẽ trả cả bài **viết về** người đó.

`tags` là ngoại lệ vì nó mô tả **nội dung**, không định danh ai — nó có hai bản
sao: một nướng vào sparse vector (chấm điểm), một ở payload (lọc).

### BM25 cho tiếng Việt

```
NFC → thường → tách âm tiết → bỏ 68 hư từ → bỏ token 1 ký tự → THÊM BIGRAM
```

Bigram bắt buộc: "giá vàng" là 2 âm tiết rời, unigram thì mất liên hệ. Bigram
không bắc qua ranh giới dòng, tránh ghép hai tag rời thành term rác.

Hai phía tính khác nhau — phía tài liệu có bão hoà tần suất (k1) và chuẩn hoá độ
dài (b), phía truy vấn chỉ dùng `tf` thô vì câu hỏi không có "độ dài tài liệu".
IDF do Qdrant nhân vào ở server nên client không phải giữ thống kê corpus.

Token → id bằng `crc32`, không dùng `hash()` của Python (bị randomize mỗi tiến
trình → index và query ra id khác nhau).

### Chỉ embed phần đổi

```python
have = vs.existing_texts(qc, ids)      # {id: sha256(text ĐÃ LƯU trong payload)}
pending = [p for p in pending if have.get(p["id"]) != sha256(p["text"])]
```

So **chính văn bản trong payload**, không so `content_hash` — hash phụ thuộc cách
băm của phiên bản code ghi ra nó, đổi cách băm là mọi point cũ đều "khác".

Băm theo **chunk** chứ không theo bài: sửa một ký tự ở đoạn 2 thì chỉ chunk đó làm
lại, không phải cả bài.

### Gom lô theo ngân sách token

Trần TPM là **30.000 token/phút** và cả lô đi trong **một** request. Lô cố định 40
chunk cho ra ~4.000 token (toàn summary) hoặc ~38.000 (toàn body) — trường hợp sau
vượt trần và 429 tức khắc. Nên gom theo `EMBED_BATCH_TOKENS` (20.000);
`EMBED_BATCH` chỉ còn là chặn phụ trên số chunk.

### Bể API key

`newscrawler/gemini.py`. Mỗi key giữ **cửa sổ trượt 60 giây** ghi lại đã tiêu bao
nhiêu token. `acquire(tokens)` chỉ trả key còn đủ chỗ cho lô sắp gửi và **đặt chỗ
trước**; không key nào đủ thì chờ tới lúc cửa sổ nhả ra. Xoay vòng chủ động, không
đợi 429 — đợi lỗi rồi mới đổi key thì mỗi lô đốt một request hỏng.

| trần | đơn vị | xử lý |
|---|---|---|
| TPM ~30.000/phút/project | token | ngân sách cửa sổ trượt |
| RPD 1.000/ngày/project | request | chờ reset hoặc thêm project |

Đường **tương tác** (giao diện, CLI) đặt `max_wait=20s`: thà báo lỗi ngay còn hơn
để người dùng nhìn spinner quay vài phút. Job nền thì chờ bao lâu cũng được.

### Dọn point mồ côi

Point id gắn với `chunk_idx`, nên khi số chunk của một bài **giảm** (báo sửa bài
ngắn lại, đổi `CHUNK_MAX_TOKENS`, `compact` bỏ dòng), point ở chỉ số lớn hơn không
bị ghi đè và vẫn trả về văn bản cũ. `prune_orphans()` quét toàn collection lấy id
rồi xoá phần không còn thuộc bài nào.

## Tầng 3 — Truy vấn

`newscrawler/ask.py`. Giao diện và CLI đi cùng một đường nên không lệch kết quả.

```
câu hỏi + bộ lọc
   ├─ embed(câu hỏi, RETRIEVAL_QUERY)  +  bm25.query_vector(câu hỏi)
   │
   ├─ hybrid_search — MỘT request, hai nhánh song song ở server
   │     Prefetch dense  96 ứng viên ─┐  filter áp TRONG từng nhánh,
   │     Prefetch sparse 96 ứng viên ─┤  TRƯỚC khi chấm điểm
   │                                   ▼
   │                          RRF fusion → 24 chunk
   │
   ├─ cosine THẬT = dot(qvec, vec)     ← lấy kèm vector, không tốn request
   ├─ group_articles()  24 chunk → 8 BÀI
   └─ ba cổng chống bịa → tóm tắt (tuỳ chọn) + thẻ bài
```

### Bộ lọc payload

Filter nằm **bên trong từng nhánh Prefetch** — Qdrant loại point trước khi chấm
điểm, không phải lọc sau. Lọc sau thì 96 ứng viên có thể còn 0.

| điều kiện | kiểu khớp |
|---|---|
| `source`, `category`, `author_name` | `MatchValue` — chính xác |
| `tags[]`, `source_attribution[]` | `MatchValue` trên mảng → khớp bất kỳ phần tử |
| `days` | `Range(gte=…)` trên `published_ts` |

Tất cả là `must` (giao). **Bộ lọc chỉ đến từ dropdown / cờ CLI** — gõ "tin thể thao
mới nhất" vào ô tìm kiếm không tạo ra bộ lọc nào, chữ chỉ đi vào dense + BM25 để
chấm điểm.

### Hai nhánh bù nhau

Dense bắt ngữ nghĩa ("xe điện" ~ "ô tô chạy pin") nhưng hay trượt tên riêng và số
liệu. BM25 bắt khớp chữ chính xác nhưng nhiễu nặng ở hạng thấp. Hợp lại thì bài
BM25 đứng đầu được vào top, còn rác BM25 bị dense đẩy xuống.

Hai thang điểm không cộng được (cosine 0–1 vs BM25 không chặn trên), nên RRF **chỉ
dùng thứ hạng**:

```
điểm = Σ 1/(k + hạng)        k = 2, hạng đếm từ 0
```

Hệ quả cần nhớ: RRF **luôn cho hạng 0 điểm 0.5**, bất kể câu hỏi có liên quan hay
không. Vì thế cổng chống bịa không dùng được điểm RRF.

### Gộp chunk về bài

Lấy dư `top_k × 3` chunk rồi mới cắt ở tầng **bài** — không lấy dư thì một bài dài
4 chunk chiếm 4/8 chỗ rồi hiện ra như một nguồn.

Điểm bài = chunk **mạnh nhất**, không cộng dồn (cộng dồn thiên vị bài dài chỉ vì nó
nhiều chunk). Chunk cùng bài nhập thành một khối trong prompt, đánh số `[1]`,
`[2]`… để LLM thấy một nguồn chứ không phải ba.

### Sắp xếp

Mặc định: độ khớp trước, **ngang điểm thì bài mới đứng trên**. RRF hay ra điểm
trùng khít nên chênh dưới `ASK_SCORE_EPS` coi như bằng nhau, ngày quyết định. Thời
gian chỉ phá thế hoà, không bao giờ hạ điểm bài đang khớp tốt hơn.

Chế độ "Mới nhất trước" lấy dư 8× vì bài mới nhất của kho không nhất thiết nằm
trong nhóm khớp cao nhất.

### Ba cổng chống bịa

Tất cả nằm ở `ask.py`, **không** ở giao diện — đặt ở tầng UI thì gọi qua CLI hay
API sẽ không có cổng nào.

**1. Sàn cosine, chặn trước khi gọi LLM.** Lấy kèm vector dense trong chính truy
vấn đang chạy rồi tính tích vô hướng tại chỗ (cả hai đã chuẩn hoá L2 → chính là
cosine), không thêm request nào. Dưới `ASK_MIN_COSINE` thì **không gọi LLM lần
nào** — vector luôn trả về thứ "gần nhất", không có sàn thì câu hỏi vô nghĩa vẫn ra
8 bài và LLM vẫn có cái để bịa.

**2. Chuỗi từ chối cố định.** Model phải in đúng `KHÔNG ĐỦ DỮ LIỆU` — chuỗi cố
định vì "tôi không tìm thấy" viết mỗi lần một kiểu thì code không nhận ra. Đây là
cổng duy nhất phán xét được *"đoạn này có trả lời câu hỏi không"*: bài có thể rất
giống câu hỏi mà vẫn không chứa câu trả lời.

**3. Bắt buộc trích dẫn, cắt bằng code.** Dòng nào không gắn được `[n]` hợp lệ thì
xoá; không còn dòng nào thì không có câu trả lời. Nhận cả `[1][2]` lẫn `[1, 2, 3]`.
Lớp này bắt được thứ prompt không bắt được: model bịa số trích dẫn.

Kết quả trả về:

```python
{"articles": [...], "answer": str|None, "grounded": bool, "best_cos": float}
```

`articles` **luôn** là phần chính. `answer` chỉ tồn tại khi qua cả ba cổng, và mặc
định tắt — bật thì tốn 2 request Gemini/câu thay vì 1, danh sách bài y hệt nhau.

---

# Cách dùng

## Giao diện

```bash
docker compose --profile chat up -d chatbot     # http://localhost:8501
```

Kết quả chính là **danh sách thẻ bài báo**: ảnh, tiêu đề, sapo, tác giả, nguồn,
ngày, chuyên mục, link. Tóm tắt của AI chỉ là phần phụ và bị ẩn khi không đủ căn cứ.

Thanh bên: Báo · Chuyên mục · Trong vòng N ngày · Tác giả · Tag · Dẫn lại từ · Số
bài hiển thị · Sắp xếp · Kèm tóm tắt của AI.

Ảnh thumbnail stream thẳng từ CDN của báo — server không tải, không lưu.

## CLI

```bash
# Hỏi
docker compose run --rm crawler -m newscrawler.ask "U20 Việt Nam thi đấu thế nào?"
docker compose run --rm crawler -m newscrawler.ask --no-llm --category "Thể thao" \
    --days 7 --sort recent "lịch thi đấu"

# Xem dữ liệu
docker compose run --rm crawler -m newscrawler.peek -n 15       # 15 bài mới nhất
docker compose run --rm crawler -m newscrawler.peek --stats
docker compose run --rm crawler -m newscrawler.peek --id <article_id>   # toàn văn

# Xuất CSV
MSYS_NO_PATHCONV=1 docker compose run --rm -v "$PWD/out:/out" crawler \
    -m newscrawler.peek --csv /out/tin.csv --limit 0
```

Cờ của `ask`: `--source --category --days --author --tag --attribution --top-k
--sort --no-llm`.

## SQL

```bash
docker exec -it trino trino --catalog iceberg --schema bronze
```

Hoặc DBeaver/DataGrip: `jdbc:trino://localhost:8080`, catalog `iceberg`, schema
`bronze`, user bất kỳ, không mật khẩu.

```sql
SELECT source, count(*) FROM articles GROUP BY 1;

-- metadata Iceberg (nhớ nháy kép vì có ký tự $)
SELECT committed_at, operation FROM "articles$snapshots" ORDER BY 1 DESC LIMIT 5;

-- du hành thời gian
SELECT count(*) FROM articles FOR TIMESTAMP AS OF TIMESTAMP '2026-09-08 04:00:00 UTC';

-- thay đổi giữa hai snapshot
SELECT _change_type, count(*) FROM TABLE(iceberg.system.table_changes(
    schema_name => 'bronze', table_name => 'articles',
    start_snapshot_id => <cũ>, end_snapshot_id => <mới>)) GROUP BY 1;
```

> Truy vấn có chữ tiếng Việt trên Git Bash phải đưa qua file (`docker cp q.sql
> trino:/tmp/ && docker exec trino trino -f /tmp/q.sql`) — truyền qua `--execute`
> sẽ hỏng mã hoá. Tương tự, thêm `MSYS_NO_PATHCONV=1` khi truyền đường dẫn Unix.

## Makefile

```
make up        make check      # service + DAG run + số dòng từng nguồn
make down      make dag        # trạng thái task của DAG run mới nhất
make ps        make dag-logs   # log crawl (số bài mỗi nguồn)
make logs      make peek / stats / schema
make urls      make sql Q="SELECT ..."   ·   make trino
               make compact / embed / ask Q="..."
```

## Airflow

<http://localhost:8085>, tài khoản trong `.env` (`AIRFLOW_ADMIN_*`).

DAG `news_crawl`, `0 2 * * *` giờ VN, `catchup=False`, `max_active_runs=1`:

```
crawl[vnexpress]  ─┐
crawl[vietnamnet] ─┤  4 mapped task SONG SONG (DockerOperator)
crawl[tuoitre]    ─┤  mỗi nguồn 1 container, 1 append vào Bronze
crawl[thanhnien]  ─┘
        └────────────►  embed   (chạy sau khi CẢ 4 xong)
```

Dùng `DockerOperator` để cách ly phụ thuộc: `airflow/requirements.txt` chỉ có
provider, còn `pyiceberg`/`qdrant-client`/`google-genai` nằm trong image
`crawler:0.1`. Cùng image chạy được từ dòng lệnh lẫn từ DAG nên hành vi giống hệt.

> Đổi biến môi trường trong `docker-compose.yml` phải **`--force-recreate`**, không
> phải `restart` — env nướng vào container lúc *tạo*:
> `docker compose up -d --force-recreate airflow-scheduler`

---

# Cấu hình

Toàn bộ trong `.env` (xem `.env.example`). Những knob đáng chú ý:

| biến | mặc định | ý nghĩa |
|---|---|---|
| `GEMINI_API_KEYS` | — | nhiều key, phẩy ngăn cách. Mỗi key một project |
| `GEMINI_TPM_LIMIT` / `_SAFETY` | 30000 / 0.8 | ngân sách token/phút/project |
| `EMBED_BATCH_TOKENS` | 20000 | trần token cho **một** request — ràng buộc thật |
| `EMBED_BATCH` | 40 | trần trên *số chunk*, chặn phụ |
| `EMBED_DIM` | 768 | Matryoshka. Đổi = **embed lại toàn bộ** |
| `CHUNK_MAX_TOKENS` / `_SINGLE_TOKENS` | 1200 / 1400 | đổi = **embed lại toàn bộ** |
| `BM25_K1` / `_B` / `_AVGDL` | 1.2 / 0.75 / 645 | tham số BM25 |
| `ASK_TOP_K` | 8 | số **bài** hiển thị |
| `ASK_MIN_COSINE` | 0.70 | sàn cổng 1 |
| `ASK_SCORE_EPS` | 0.02 | chênh dưới ngưỡng = ngang điểm → bài mới thắng |
| `ASK_OVERFETCH` | 3 | lấy dư ở tầng chunk trước khi gộp về bài |
| `ASK_MAX_WAIT` | 20 | giây chờ quota ở đường tương tác rồi báo lỗi |

Sửa `.env` xong chỉ cần `docker compose --profile chat up -d --force-recreate
chatbot`, không phải build lại image.

---

# Vận hành

## Công cụ sửa chữa (chạy tay, không trong DAG)

```bash
# Parse lại toàn văn từ HTML thô — khi vá lỗi bóc tách văn bản
docker compose run --rm --no-deps crawler -m newscrawler.repair --content          # thử
docker compose run --rm --no-deps crawler -m newscrawler.repair --content --apply

# Sửa nhãn sai (author_name / source_attribution / sub_category / topic)
docker compose run --rm --no-deps crawler -m newscrawler.repair
docker compose run --rm --no-deps crawler -m newscrawler.repair --check 40   # đối chứng

# Khử trùng + chuẩn hoá + tính lại cột dẫn xuất
docker compose run --rm --no-deps crawler -m newscrawler.compact
```

> Cả ba ghi bằng `overwrite`. **Chỉ chạy khi không có crawl nào đang chạy** —
> Bronze có 4 task `append` song song.

## Bảo trì Iceberg

`DELETE` không giải phóng dung lượng — Iceberg ghi thêm *delete file*, Parquet cũ
vẫn nguyên. Muốn thu hồi thật:

```sql
ALTER TABLE iceberg.bronze.articles EXECUTE optimize;
ALTER TABLE iceberg.bronze.articles EXECUTE expire_snapshots(retention_threshold => '7d');
ALTER TABLE iceberg.bronze.articles EXECUTE remove_orphan_files(retention_threshold => '7d');
```

> `expire_snapshots` xoá luôn khả năng du hành thời gian về các mốc đã xoá.

## Sức chứa

Trần **1.000 chunk/ngày/project**. Nhịp hằng ngày ~1.200 chunk (≈500 bài mới) nên
2 project là đủ; chỉ backfill toàn bộ mới cần nhiều hơn.

---

# Cấu trúc

```
crawler/
├── chat_app.py                 Streamlit — CHỈ hiển thị, không chứa logic
└── newscrawler/
    ├── main.py                 điều phối cào: nguồn × chuyên mục
    ├── fetcher.py              HTTP có giãn nhịp, retry
    ├── article.py              dataclass Article — schema chung mọi nguồn
    ├── sources/                base.py + lớp con theo từng báo
    ├── raw_storage.py          HTML thô (gzip) ↔ MinIO
    ├── storage.py              ghi Bronze, khử trùng, retry commit
    ├── chunker.py              cắt chunk: summary + body chia đều
    ├── bm25.py                 sparse vector tiếng Việt (unigram + bigram)
    ├── gemini.py               bể API key: ngân sách token/phút
    ├── vectorstore.py          Qdrant: collection, payload index, RRF
    ├── embed.py                Bronze → chunk → Qdrant (tăng dần)
    ├── ask.py                  truy hồi + 3 cổng chống bịa + CLI
    ├── peek.py                 xem/xuất dữ liệu Bronze
    ├── compact.py              dọn bảng tại chỗ (chạy tay)
    └── repair.py               parse lại từ HTML thô (chạy tay)

airflow/dags/news_crawl.py      DAG: crawl (4 song song) >> embed
trino/etc/                      cấu hình Trino
postgres/init/                  tạo 2 database lúc khởi tạo lần đầu
```

Chi tiết crawler: [crawler/README.md](crawler/README.md).
