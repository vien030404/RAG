"""DAG cào tin hằng ngày: nhiều nguồn -> HTML thô + Iceberg Bronze -> Qdrant.

Mỗi task chạy image `crawler:0.1` như một sibling container thông qua
DockerOperator. Bước crawl dùng dynamic task mapping, một mapped task cho mỗi
nguồn, chạy song song. Cách chia này vừa nhanh hơn vừa cô lập lỗi: một báo đổi
layout hay chặn request không kéo theo các báo còn lại, và mỗi nguồn có log cùng
lịch retry riêng.

Mỗi task ghi một lần append cho nguồn của nó vào bảng dùng chung
`bronze.articles`, phân biệt qua cột `source`. Khi các task commit đồng thời,
`storage.save_articles` tự thử lại nếu gặp xung đột.

Lịch chạy 02:00 mỗi ngày. DAG không truyền logical date xuống crawler: crawler
tự lấy ngày hôm trước theo giờ Việt Nam tại thời điểm container chạy. Để cào một
ngày khác hoặc cào toàn bộ, đặt Airflow Variable `crawl_date` theo định dạng
YYYY-MM-DD hoặc giá trị "all".
"""
from __future__ import annotations

import os
from datetime import timedelta

import pendulum
from airflow import DAG
from airflow.providers.docker.operators.docker import DockerOperator

# Khai báo múi giờ Việt Nam để mốc 02:00 trong `schedule` là giờ địa phương chứ
# không phải UTC.
LOCAL_TZ = pendulum.timezone("Asia/Ho_Chi_Minh")

# Mạng của compose stack. Task phải nằm trong mạng này để phân giải được các tên
# `minio` và `iceberg-rest` khi chạy như sibling container.
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

# Danh sách này phải khớp registry SOURCES trong `newscrawler/sources`.
#
# dantri nằm ngoài danh sách. Trang chủ và trang listing vẫn trả HTTP 200, nhưng
# trang bài có cơ chế chống bot: request đầu thành công, các request sau trả
# HTTP 428 ngay lập tức, kể cả khi giãn nhịp lên 3 giây mỗi request. Đây là
# chính sách truy cập của trang chứ không phải sự cố mạng, nên nguồn được bỏ ra
# thay vì tìm cách đi vòng. Parser `sources/dantri.py` vẫn giữ lại để dùng được
# ngay nếu chính sách đó thay đổi.
SOURCES = ["vnexpress", "vietnamnet", "tuoitre", "thanhnien"]

# Thông tin kết nối catalog và object store, dùng chung cho mọi task.
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
    # Để trống thì crawler tự lấy ngày hôm trước theo giờ Việt Nam tại thời
    # điểm container chạy. Đặt Airflow Variable `crawl_date` theo định dạng
    # YYYY-MM-DD, hoặc giá trị "all", để chỉ định ngày khác khi cào bù.
    "CRAWL_DATE": "{{ var.value.get('crawl_date', '') }}",
    # Để trống thì lấy mọi chuyên mục; đặt Airflow Variable để thu hẹp phạm vi.
    "CRAWL_CATEGORIES": "{{ var.value.get('crawl_categories', '') }}",
}

# Tham số Docker dùng chung: crawl và embed chạy trên cùng một image.
_DOCKER_ARGS = dict(
    image="crawler:0.1",
    force_pull=False,                 # image build tại máy, không đẩy lên registry
    docker_url="unix://var/run/docker.sock",
    network_mode=LAKEHOUSE_NETWORK,
    auto_remove="force",
    mount_tmp_dir=False,              # bind-mount tmp của host lỗi trên Docker Desktop
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
    max_active_tasks=5,              # trần số container chạy song song
    tags=["ingestion", "crawler", "multi-source"],
) as dag:
    # Một mapped task cho mỗi nguồn: crawl[0..N] chạy song song, mỗi task nhận
    # đúng một giá trị CRAWL_SOURCES.
    crawl = DockerOperator.partial(
        task_id="crawl",
        **_DOCKER_ARGS,
    ).expand(
        environment=[{**_CRAWL_ENV, "CRAWL_SOURCES": s} for s in SOURCES],
    )

    # Bronze -> chunk -> embedding -> Qdrant, chạy tăng dần. Point id ổn định
    # theo (source, article_id, chunk_idx), nên chỉ những bài mới hoặc bài đã
    # đổi nội dung mới phải gọi API.
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
