# Lakehouse Pipeline

Web Crawl → Lakehouse, built on an open lakehouse stack and orchestrated
with Docker Compose (no Kubernetes).

```
Crawler ─▶ Iceberg Bronze ─▶ dbt+Trino ─▶ Iceberg Silver ─▶ dbt+Trino ─▶ Iceberg Gold
              │                                                             │
              └────────── Iceberg REST Catalog (shared, warehouse on MinIO) ┘
                                     Orchestration: Airflow
```

> **Status:** infrastructure layer is up and verified. The **crawler**
> (`crawler/`) is a working VnExpress news crawler that writes **directly into
> the Iceberg Bronze table** `bronze.vnexpress_articles` (queryable via Trino) —
> see [crawler/README.md](crawler/README.md). The **dbt** transformation layer
> (`dbt/`, Silver→Gold) is still a placeholder, to be built next.

## Components

| Service         | Role                                             | URL / Port (host)            |
| --------------- | ------------------------------------------------ | ---------------------------- |
| `postgres`      | Shared metadata (Iceberg catalog, Airflow)       | `localhost:5432`             |
| `minio`         | S3-compatible object storage (Iceberg `warehouse`)| `localhost:9000` (API), `9001` (console) |
| `minio-init`    | Creates the `warehouse` bucket, then exits        | —                       |
| `iceberg-rest`  | Iceberg REST Catalog (`apache/iceberg-rest-fixture` + Postgres JDBC backend, MinIO storage) | `localhost:8181` |
| `trino`         | SQL query engine (Iceberg connector)             | `localhost:8080`             |
| `airflow-*`     | Orchestration (LocalExecutor)                     | `localhost:8085`            |
| `crawler`       | Web crawler → Iceberg Bronze (on-demand job, profile `crawler`) | — (see [crawler/](crawler/)) |

## Prerequisites

- Docker Desktop (Docker Engine 24+ / Compose v2) running
- ~8 GB RAM available to Docker (Trino + Airflow are the heavy ones)

## Quick start

```bash
# 1. Create your local env file (edit secrets/ports as needed)
cp .env.example .env

# 2. Build custom images (Airflow) and start everything
docker compose up -d --build

# 3. Watch it come up
docker compose ps
docker compose logs -f
```

Using `make` (Git Bash / WSL on Windows):

```bash
make up      # build + start
make ps      # status
make urls    # print all service URLs
make logs    # tail everything
make down    # stop (keeps data)
make clean   # stop + delete all volumes (destructive)
```

## Default credentials

All configurable in `.env`. Defaults are for **local development only**.

| Service   | User         | Password     |
| --------- | ------------ | ------------ |
| MinIO     | `minioadmin` | `minioadmin` |
| Airflow   | `admin`      | `admin`      |
| Postgres  | `postgres`   | `postgres`   |

## Connecting the pieces

**Trino → Iceberg** is pre-wired via `trino/etc/catalog/iceberg.properties`
(catalog name `iceberg`). Test it:

```bash
docker compose exec trino trino --execute "SHOW SCHEMAS FROM iceberg"
```

**Iceberg REST Catalog** is reachable at `http://iceberg-rest:8181` inside the
compose network (`http://localhost:8181` from the host). dbt (added later)
should point its Trino profile at the `iceberg` catalog.

## Layout

```
.
├── docker-compose.yml         # the whole stack
├── .env / .env.example        # versions, ports, credentials
├── Makefile                   # convenience targets
├── postgres/init/             # creates iceberg_catalog / airflow DBs
├── trino/etc/                 # Trino node + Iceberg catalog config
├── airflow/                   # Dockerfile, requirements, dags/ plugins/ config/
├── crawler/                   # VnExpress crawler container → Iceberg Bronze
└── dbt/                       # (placeholder) dbt-trino bronze/silver/gold models
```

## Notes

- **Image versions** are pinned in `.env`. Bump them there in one place.
- **Metadata isolation:** one Postgres instance hosts two separate databases
  (`iceberg_catalog`, `airflow`), created on first startup by
  `postgres/init/01-create-databases.sh`.
- **Credentials stay in sync:** Trino reads MinIO keys from the container
  environment (`${ENV:AWS_ACCESS_KEY_ID}`), so changing them in `.env` is enough.
- **Resetting state:** `docker compose down -v` deletes all volumes (Postgres
  metadata, MinIO objects, Trino data) for a clean slate.
