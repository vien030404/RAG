"""Daily VnExpress crawl → Iceberg Bronze (`bronze.vnexpress_articles`).

Runs the standalone `crawler:0.1` image as a sibling container (DockerOperator),
so the crawl executes in its own container — Airflow only orchestrates it.
The crawler writes directly to the Iceberg Bronze table via the REST catalog.

Schedule: 02:00 every day. For a run firing at 02:00 on day D, Airflow's logical
date ``{{ ds }}`` is the previous day (D-1), i.e. the day that just completed —
exactly what we want to crawl. Category scope is controlled by the Airflow
Variable ``vnexpress_crawl_categories`` (comma-separated names; empty = all).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from airflow import DAG
from airflow.providers.docker.operators.docker import DockerOperator

# Compose network so the crawler can resolve `minio`, matching `docker run --network`.
LAKEHOUSE_NETWORK = os.getenv("LAKEHOUSE_DOCKER_NETWORK", "project-1_lakehouse")

# Iceberg REST catalog + MinIO connection — read from the scheduler's env
# (set by compose from .env), with safe local-dev defaults.
ICEBERG_REST_URI = os.getenv("LAKEHOUSE_ICEBERG_REST_URI", "http://iceberg-rest:8181")
ICEBERG_WAREHOUSE = os.getenv("LAKEHOUSE_ICEBERG_WAREHOUSE", "s3://warehouse/")
MINIO_ENDPOINT = os.getenv("LAKEHOUSE_S3_ENDPOINT", "http://minio:9000")
MINIO_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY_ID", "minioadmin")
MINIO_SECRET_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "minioadmin")
MINIO_REGION = os.getenv("AWS_REGION", "us-east-1")

default_args = {
    "owner": "data-eng",
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
}

with DAG(
    dag_id="vnexpress_crawl",
    description="Crawl VnExpress articles for the logical date into Iceberg Bronze.",
    schedule="0 2 * * *",            # 02:00 daily
    start_date=datetime(2026, 7, 1),
    catchup=False,
    default_args=default_args,
    max_active_runs=1,
    tags=["ingestion", "crawler", "vnexpress"],
) as dag:
    crawl = DockerOperator(
        task_id="crawl",
        image="crawler:0.1",
        force_pull=False,             # local image, not in a registry
        docker_url="unix://var/run/docker.sock",
        network_mode=LAKEHOUSE_NETWORK,
        auto_remove="force",
        mount_tmp_dir=False,          # avoid host tmp bind-mount issues on Docker Desktop
        environment={
            "ICEBERG_REST_URI": ICEBERG_REST_URI,
            "ICEBERG_WAREHOUSE": ICEBERG_WAREHOUSE,
            "MINIO_ENDPOINT": MINIO_ENDPOINT,
            "MINIO_ACCESS_KEY": MINIO_ACCESS_KEY,
            "MINIO_SECRET_KEY": MINIO_SECRET_KEY,
            "AWS_REGION": MINIO_REGION,
            "BRONZE_NAMESPACE": "bronze",
            "BRONZE_TABLE": "vnexpress_articles",
            # logical date (D-1 for a 02:00 daily run) = the completed day to crawl
            "CRAWL_DATE": "{{ ds }}",
            # empty => all categories; set Airflow Variable to crawl a subset
            "CRAWL_CATEGORIES": "{{ var.value.get('vnexpress_crawl_categories', '') }}",
        },
    )
