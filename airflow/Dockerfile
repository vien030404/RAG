ARG AIRFLOW_VERSION=2.10.2
FROM apache/airflow:${AIRFLOW_VERSION}

# Re-declare ARGs so they are available after FROM.
ARG AIRFLOW_VERSION
# Base image runs Python 3.12; used to select the matching constraints file.
ARG PYTHON_VERSION=3.12

# Providers the pipeline will use: Trino (query/orchestrate dbt models),
# Amazon (S3/MinIO), plus clients for Trino and MinIO.
#
# IMPORTANT: install against Airflow's official constraints so the resolver
# never upgrades apache-airflow itself (without this, the latest providers
# drag in Airflow 3.x and break the `airflow` CLI / entrypoint).
COPY requirements.txt /requirements.txt
RUN pip install --no-cache-dir -r /requirements.txt \
    --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${PYTHON_VERSION}.txt"
