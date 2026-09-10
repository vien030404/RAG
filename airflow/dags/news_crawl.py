"""Daily multi-source news crawl → raw HTML + Iceberg Bronze (`bronze.articles`).

Chạy image `crawler:0.1` như sibling container (DockerOperator). Mỗi NGUỒN là 1
mapped task chạy **song song** (dynamic task mapping) → nhanh hơn & cô lập lỗi:
1 báo hỏng không kéo theo báo khác, retry riêng, log riêng từng nguồn.

Mỗi task ghi **1 append / nguồn** vào bảng thống nhất `bronze.articles`
(phân biệt qua cột `source`); `storage.save_articles` tự retry nếu commit trùng
khi các task ghi đồng thời.

Schedule: 02:00 mỗi ngày. KHÔNG truyền logical date; crawler tự lấy "hôm qua"
theo giờ VN tại thời điểm container chạy. Đặt Airflow Variable `crawl_date`
(YYYY-MM-DD | all) để override khi cần backfill / crawl toàn bộ.
"""
from __future__ import annotations

import os
from datetime import timedelta

import pendulum
from airflow import DAG
from airflow.providers.docker.operators.docker import DockerOperator

# Múi giờ Việt Nam để "2h sáng" là giờ địa phương (không phải UTC).
LOCAL_TZ = pendulum.timezone("Asia/Ho_Chi_Minh")

# Compose network để crawler resolve `minio`/`iceberg-rest` như sibling container.
LAKEHOUSE_NETWORK = os.getenv("LAKEHOUSE_DOCKER_NETWORK", "project-1_lakehouse")

ICEBERG_REST_URI = os.getenv("LAKEHOUSE_ICEBERG_REST_URI", "http://iceberg-rest:8181")
ICEBERG_WAREHOUSE = os.getenv("LAKEHOUSE_ICEBERG_WAREHOUSE", "s3://warehouse/")
MINIO_ENDPOINT = os.getenv("LAKEHOUSE_S3_ENDPOINT", "http://minio:9000")
MINIO_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY_ID", "minioadmin")
MINIO_SECRET_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "minioadmin")
MINIO_REGION = os.getenv("AWS_REGION", "us-east-1")
RAW_BUCKET = os.getenv("LAKEHOUSE_RAW_BUCKET", "raw")

# RAG (chunk -> embedding Gemini -> Qdrant)
QDRANT_URL = os.getenv("LAKEHOUSE_QDRANT_URL", "http://qdrant:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "")
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "news_chunks")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_API_KEYS = os.getenv("GEMINI_API_KEYS", "")
GEMINI_KEY_COOLDOWN = os.getenv("GEMINI_KEY_COOLDOWN", "60")
GEMINI_TPM_LIMIT = os.getenv("GEMINI_TPM_LIMIT", "30000")
GEMINI_TPM_SAFETY = os.getenv("GEMINI_TPM_SAFETY", "0.8")
GEMINI_RPM_LIMIT = os.getenv("GEMINI_RPM_LIMIT", "100")
EMBED_BATCH = os.getenv("EMBED_BATCH", "40")
GEMINI_EMBED_MODEL = os.getenv("GEMINI_EMBED_MODEL", "gemini-embedding-001")
EMBED_DIM = os.getenv("EMBED_DIM", "768")

# Phải khớp registry newscrawler/sources (SOURCES).
# dantri VẪN GỠ. Kiểm tra 2026-09-04: blackhole IP cũ đã hết (trang chủ + trang
# listing trả HTTP 200), nhưng trang BÀI nay có bot protection — request đầu
# 200, các request sau trả HTTP 428 tức thì kể cả khi giãn 3s/request. Đó là
# chính sách truy cập của trang, không phải lỗi mạng; không lách.
SOURCES = ["vnexpress", "vietnamnet", "tuoitre", "thanhnien"]

# Kết nối catalog/object store — chung cho mọi task.
_STORAGE_ENV = {
    "ICEBERG_REST_URI": ICEBERG_REST_URI,
    "ICEBERG_WAREHOUSE": ICEBERG_WAREHOUSE,
    "MINIO_ENDPOINT": MINIO_ENDPOINT,
    "MINIO_ACCESS_KEY": MINIO_ACCESS_KEY,
    "MINIO_SECRET_KEY": MINIO_SECRET_KEY,
    "AWS_REGION": MINIO_REGION,
    "BRONZE_NAMESPACE": "bronze",
    "BRONZE_TABLE": "articles",
}

_CRAWL_ENV = {
    **_STORAGE_ENV,
    "RAW_BUCKET": RAW_BUCKET,
    # Rỗng => crawler tự lấy "hôm qua" theo giờ VN lúc container chạy.
    # Đặt Airflow Variable `crawl_date` (YYYY-MM-DD | all) để override khi backfill.
    "CRAWL_DATE": "{{ var.value.get('crawl_date', '') }}",
    # rỗng => tất cả category; đặt Airflow Variable để giới hạn
    "CRAWL_CATEGORIES": "{{ var.value.get('crawl_categories', '') }}",
}

# Tham số Docker dùng chung cho mọi task (crawl + embed cùng 1 image).
_DOCKER_ARGS = dict(
    image="crawler:0.1",
    force_pull=False,                 # image local, không ở registry
    docker_url="unix://var/run/docker.sock",
    network_mode=LAKEHOUSE_NETWORK,
    auto_remove="force",
    mount_tmp_dir=False,              # tránh lỗi host tmp bind-mount trên Docker Desktop
)

default_args = {
    "owner": "data-eng",
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
}

with DAG(
    dag_id="news_crawl",
    description="Crawl báo (song song) → raw HTML + Bronze → chunk/embed → Qdrant (RAG).",
    schedule="0 2 * * *",            # 02:00 mỗi ngày (giờ VN)
    start_date=pendulum.datetime(2026, 7, 1, tz=LOCAL_TZ),
    catchup=False,
    default_args=default_args,
    max_active_runs=1,
    max_active_tasks=5,              # tối đa 5 container crawler song song
    tags=["ingestion", "crawler", "multi-source"],
) as dag:
    # 1 mapped task / nguồn: crawl[0..N] chạy song song, mỗi task 1 CRAWL_SOURCES.
    crawl = DockerOperator.partial(
        task_id="crawl",
        **_DOCKER_ARGS,
    ).expand(
        environment=[{**_CRAWL_ENV, "CRAWL_SOURCES": s} for s in SOURCES],
    )

    # Bronze -> chunk -> embedding (Gemini) -> Qdrant. Tăng dần: point id ổn định
    # theo (source, article_id, chunk_idx) nên chỉ embed bài mới / bài đổi nội dung.
    embed = DockerOperator(
        task_id="embed",
        command=["-m", "newscrawler.embed"],
        environment={
            **_STORAGE_ENV,
            "QDRANT_URL": QDRANT_URL,
            "QDRANT_API_KEY": QDRANT_API_KEY,
            "QDRANT_COLLECTION": QDRANT_COLLECTION,
            "GEMINI_API_KEY": GEMINI_API_KEY,
            "GEMINI_API_KEYS": GEMINI_API_KEYS,
            "GEMINI_KEY_COOLDOWN": GEMINI_KEY_COOLDOWN,
            "GEMINI_TPM_LIMIT": GEMINI_TPM_LIMIT,
            "GEMINI_TPM_SAFETY": GEMINI_TPM_SAFETY,
            "GEMINI_RPM_LIMIT": GEMINI_RPM_LIMIT,
            "EMBED_BATCH": EMBED_BATCH,
            "GEMINI_EMBED_MODEL": GEMINI_EMBED_MODEL,
            "EMBED_DIM": EMBED_DIM,
        },
        **_DOCKER_ARGS,
    )

    crawl >> embed
