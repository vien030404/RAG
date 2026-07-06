#!/bin/bash
# ============================================================================
# Creates the separate metadata databases + owners used by the lakehouse.
# Runs once, on first initialization of the Postgres data volume.
# Values are injected from the compose environment (see docker-compose.yml).
# ============================================================================
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    -- Iceberg REST catalog metadata
    CREATE USER ${ICEBERG_CATALOG_USER} WITH PASSWORD '${ICEBERG_CATALOG_PASSWORD}';
    CREATE DATABASE ${ICEBERG_CATALOG_DB} OWNER ${ICEBERG_CATALOG_USER};

    -- Airflow metadata
    CREATE USER ${AIRFLOW_DB_USER} WITH PASSWORD '${AIRFLOW_DB_PASSWORD}';
    CREATE DATABASE ${AIRFLOW_DB} OWNER ${AIRFLOW_DB_USER};
EOSQL

echo "[postgres-init] Created databases: ${ICEBERG_CATALOG_DB}, ${AIRFLOW_DB}"
