# ============================================================================
# Convenience wrapper around docker compose for the lakehouse stack.
# On Windows, run these via Git Bash / WSL, or use the raw `docker compose`
# commands shown in the README.
# ============================================================================
COMPOSE = docker compose

.PHONY: help init build up down restart ps logs clean urls \
        logs-trino logs-airflow logs-iceberg

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

init: ## Create .env from .env.example if missing
	@test -f .env || (cp .env.example .env && echo "Created .env")

build: ## Build the custom Airflow image
	$(COMPOSE) build

up: init ## Build (if needed) and start the whole stack in the background
	$(COMPOSE) up -d --build

down: ## Stop and remove containers (keeps volumes/data)
	$(COMPOSE) down

restart: ## Restart all services
	$(COMPOSE) restart

ps: ## Show service status
	$(COMPOSE) ps

logs: ## Tail logs from all services
	$(COMPOSE) logs -f --tail=100

logs-trino: ## Tail Trino logs
	$(COMPOSE) logs -f trino

logs-airflow: ## Tail Airflow logs
	$(COMPOSE) logs -f airflow-webserver airflow-scheduler

logs-iceberg: ## Tail Iceberg REST catalog logs
	$(COMPOSE) logs -f iceberg-rest

clean: ## Stop everything AND delete all data volumes (destructive!)
	$(COMPOSE) down -v

urls: ## Print the service URLs
	@echo "MinIO console : http://localhost:$${MINIO_CONSOLE_PORT:-9001}"
	@echo "Iceberg REST  : http://localhost:$${ICEBERG_REST_PORT:-8181}/v1/config"
	@echo "Trino         : http://localhost:$${TRINO_PORT:-8080}"
	@echo "Airflow       : http://localhost:$${AIRFLOW_PORT:-8085}"
