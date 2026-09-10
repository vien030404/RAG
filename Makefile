# ============================================================================
# Convenience wrapper around docker compose for the data stack.
# On Windows, run these via Git Bash / WSL, or use the raw `docker compose`
# commands shown in the README.
# ============================================================================
COMPOSE = docker compose
# Chạy 1 module trong image crawler (dùng cho peek / compact / embed / ask).
# MSYS_NO_PATHCONV=1: Git Bash trên Windows sẽ đổi "/out/x.csv" thành đường dẫn
# Windows nếu không tắt.
RUN = MSYS_NO_PATHCONV=1 $(COMPOSE) run --rm -v "$(CURDIR)/out:/out" crawler

.PHONY: help init build up down restart ps logs clean urls \
        logs-airflow logs-iceberg \
        check dag dag-logs sql trino peek stats schema compact embed ask

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

logs-airflow: ## Tail Airflow logs
	$(COMPOSE) logs -f airflow-webserver airflow-scheduler

logs-iceberg: ## Tail Iceberg REST catalog logs
	$(COMPOSE) logs -f iceberg-rest

clean: ## Stop everything AND delete all data volumes (destructive!)
	$(COMPOSE) down -v

urls: ## Print the service URLs
	@echo "MinIO console : http://localhost:$${MINIO_CONSOLE_PORT:-9001}"
	@echo "Iceberg REST  : http://localhost:$${ICEBERG_REST_PORT:-8181}/v1/config"
	@echo "Airflow       : http://localhost:$${AIRFLOW_PORT:-8085}"
	@echo "Qdrant        : http://localhost:$${QDRANT_HTTP_PORT:-6333}/dashboard"
	@echo "Trino         : http://localhost:$${TRINO_PORT:-8080}  (jdbc:trino://localhost:8080)"

# ============================================================================
# Kiểm tra pipeline
# ============================================================================
check: ## Tổng quan: service, DAG run gần nhất, số dòng từng tầng
	@echo "── SERVICE ──────────────────────────────────────────────"
	@$(COMPOSE) ps --format 'table {{.Service}}\t{{.Status}}'
	@echo ""
	@echo "── DAG RUN (5 gần nhất) ─────────────────────────────────"
	@docker exec airflow-scheduler airflow dags list-runs -d news_crawl 2>/dev/null \
		| grep -viE 'deprecat|warn' | head -8 || echo "  (airflow chưa chạy)"
	@echo ""
	@echo "── DỮ LIỆU ──────────────────────────────────────────────"
	@docker exec trino trino --catalog iceberg --schema bronze --output-format ALIGNED \
		--execute "SELECT source, count(*) AS bai, max(published_date) AS moi_nhat \
		 FROM articles GROUP BY source ORDER BY 2 DESC" 2>/dev/null

dag: ## Trạng thái các task của DAG run mới nhất
	@RID=$$(docker exec airflow-scheduler airflow dags list-runs -d news_crawl 2>/dev/null \
		| grep -oE '(manual|scheduled)__[0-9T:+-]+' | head -1); \
	echo "run: $$RID"; \
	docker exec airflow-scheduler airflow tasks states-for-dag-run news_crawl "$$RID" 2>/dev/null \
		| grep -viE 'deprecat|warn'

dag-logs: ## Log crawl của DAG run mới nhất (số bài mỗi nguồn)
	@RID=$$(docker exec airflow-scheduler airflow dags list-runs -d news_crawl 2>/dev/null \
		| grep -oE '(manual|scheduled)__[0-9T:+-]+' | head -1); \
	docker exec airflow-scheduler bash -lc \
		'for f in $$(find /opt/airflow/logs/dag_id=news_crawl/run_id='"$$RID"' -name "*.log" 2>/dev/null); do \
		   grep -hE "NGUỒN:|] [0-9]+ bài|đã qua .*dừng|bỏ qua [0-9]+|Saved [0-9]+|Done. Tổng|⚠" $$f; done' \
		| sed -E 's/.*INFO - //'

sql: ## SQL qua Trino.  make sql Q="SELECT ... FROM articles"
	@docker exec trino trino --catalog iceberg --schema bronze \
		--output-format ALIGNED --execute "$(Q)" 2>/dev/null

trino: ## Mở Trino CLI (gõ SQL trực tiếp, `quit` để thoát)
	@docker exec -it trino trino --catalog iceberg --schema bronze

peek: ## 15 bài mới nhất
	@$(RUN) -m newscrawler.peek -n 15 2>/dev/null | grep -viE 'insecure|warn'

stats: ## Thống kê nguồn / chuyên mục / ngày
	@$(RUN) -m newscrawler.peek --stats 2>/dev/null | grep -viE 'insecure|warn'

schema: ## Danh sách cột của bronze.articles
	@$(RUN) -m newscrawler.peek --schema 2>/dev/null | grep -viE 'insecure|warn'

# ============================================================================
# Chạy tay từng chặng
# ============================================================================
compact: ## Dọn lại bronze.articles (khử trùng + tính lại cột dẫn xuất)
	@$(RUN) -m newscrawler.compact

embed: ## Embed chunk mới vào Qdrant (cần GEMINI_API_KEY)
	@$(RUN) -m newscrawler.embed

ask: ## Hỏi kho tin.  make ask Q="Giá vàng hôm qua thế nào?"
	@$(RUN) -m newscrawler.ask "$(Q)"
