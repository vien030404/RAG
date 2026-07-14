"""Daily multi-source news crawl → raw HTML + Iceberg Bronze (`bronze.articles`).

Chạy image `crawler:0.1` như sibling container (DockerOperator). Mỗi NGUỒN là 1
mapped task chạy **song song** (dynamic task mapping) → nhanh hơn & cô lập lỗi:
1 báo hỏng không kéo theo báo khác, retry riêng, log riêng từng nguồn.

Mỗi task ghi **1 append / nguồn** vào bảng thống nhất `bronze.articles`
(phân biệt qua cột `source`); `storage.save_articles` tự retry nếu commit trùng
khi các task ghi đồng thời.

Schedule: 02:00 mỗi ngày. `{{ ds }}` (logical date = ngày vừa hoàn tất) được
truyền làm `CRAWL_DATE` để lọc bài theo ngày.
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

# Phải khớp registry newscrawler/sources (SOURCES).
SOURCES = ["vnexpress", "vietnamnet", "tuoitre", "thanhnien", "dantri"]

_BASE_ENV = {
    "ICEBERG_REST_URI": ICEBERG_REST_URI,
    "ICEBERG_WAREHOUSE": ICEBERG_WAREHOUSE,
    "MINIO_ENDPOINT": MINIO_ENDPOINT,
    "MINIO_ACCESS_KEY": MINIO_ACCESS_KEY,
    "MINIO_SECRET_KEY": MINIO_SECRET_KEY,
    "AWS_REGION": MINIO_REGION,
    "RAW_BUCKET": RAW_BUCKET,
    "BRONZE_NAMESPACE": "bronze",
    "BRONZE_TABLE": "articles",
    # logical date (D-1 cho run 02:00) = ngày đã hoàn tất cần crawl
    "CRAWL_DATE": "{{ ds }}",
    # rỗng => tất cả category; đặt Airflow Variable để giới hạn
    "CRAWL_CATEGORIES": "{{ var.value.get('crawl_categories', '') }}",
}

default_args = {
    "owner": "data-eng",
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
}

with DAG(
    dag_id="news_crawl",
    description="Crawl 5 báo (song song theo nguồn) → raw HTML + Iceberg Bronze.",
    schedule="0 2 * * *",            # 02:00 mỗi ngày (giờ VN)
    start_date=pendulum.datetime(2026, 7, 1, tz=LOCAL_TZ),
    catchup=False,
    default_args=default_args,
    max_active_runs=1,
    max_active_tasks=5,              # tối đa 5 container crawler song song
    tags=["ingestion", "crawler", "multi-source"],
) as dag:
    # 1 mapped task / nguồn: crawl[0..4] chạy song song, mỗi task 1 CRAWL_SOURCES.
    DockerOperator.partial(
        task_id="crawl",
        image="crawler:0.1",
        force_pull=False,             # image local, không ở registry
        docker_url="unix://var/run/docker.sock",
        network_mode=LAKEHOUSE_NETWORK,
        auto_remove="force",
        mount_tmp_dir=False,          # tránh lỗi host tmp bind-mount trên Docker Desktop
    ).expand(
        environment=[{**_BASE_ENV, "CRAWL_SOURCES": s} for s in SOURCES],
    )
