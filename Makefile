. PHONY: dev down logs contract-check docs-check test smoke perf-gate secret-gate

# Prefer the env-file the running stack actually uses. Root `.env` and
# `deploy/compose/.env` can diverge and rotate INTERNAL_TOKEN on recreate.
COMPOSE_ENV_FILE := $(if $(wildcard deploy/compose/.env),deploy/compose/.env,.env)

dev:
	docker compose --env-file $(COMPOSE_ENV_FILE) -f deploy/compose/docker-compose.yml up --build -d

down:
	docker compose --env-file $(COMPOSE_ENV_FILE) -f deploy/compose/docker-compose.yml down

logs:
	docker compose --env-file $(COMPOSE_ENV_FILE) -f deploy/compose/docker-compose.yml logs -f

contract-check:
	docker run --rm -v "$(CURDIR):/src" -w /src python:3.12-slim python tools/contract_registry_check.py --json quality/evidence/contract-registry.json
	docker run --rm -v "$(CURDIR):/src" -w /src python:3.12-slim python -m unittest tools/test_contract_registry.py

docs-check: contract-check
	docker run --rm -v "$(CURDIR):/src" -w /src python:3.12-slim python tools/docs_consistency.py --json quality/evidence/docs-consistency.json
	docker run --rm -v "$(CURDIR):/src" -w /src python:3.12-slim python -m unittest tools/test_docs_consistency.py

test: contract-check
	docker run --rm -v "$(CURDIR):/src" -w /src python:3.12-slim python tools/docs_consistency.py --json quality/evidence/docs-consistency.json
	docker run --rm -v "$(CURDIR):/src" -w /src python:3.12-slim python -m unittest tools/test_docs_consistency.py
	docker run --rm -v "$(CURDIR):/src" -w /src python:3.12-slim python tools/open_platform_contract_gate.py --json quality/evidence/open-platform-contract-gate.json
	docker run --rm -v "$(CURDIR):/src" -w /src python:3.12-slim python -m unittest tools/test_open_platform_contract_gate.py
	node --test apps/extension/tests/manifest.test.mjs
	docker compose --env-file $(COMPOSE_ENV_FILE) -f deploy/compose/docker-compose.yml run --rm platform-api pytest -q
	docker compose --env-file $(COMPOSE_ENV_FILE) -f deploy/compose/docker-compose.yml run --rm agent-server pytest -q
	pnpm.cmd --filter @workama/web test
	pnpm.cmd --filter @workama/event-renderer test
	docker run --rm -v "$(CURDIR):/src" -w /src/apps/gateway -e GOWORK=off golang:1.26-alpine go test -mod=vendor ./...
	docker run --rm -v "$(CURDIR):/src" -w /src/apps/sandbox-agentd golang:1.26-alpine env GOWORK=off go test ./...

smoke:
	powershell -ExecutionPolicy Bypass -File tools/smoke.ps1

perf-gate:
	python deploy/perf/run_perf_gate.py

secret-gate:
	python tools/secret-gate.py

# 生产拉起：预检 .env.production（fail-fast）→ compose 双文件叠加。
# 可运维配置不在 env 文件里——部署后经控制台 /admin/platform-config 管理。
PROD_ENV_FILE := deploy/compose/.env.production
PROD_COMPOSE_FILES := -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml

prod-check:
	python tools/prod_env_check.py --env-file $(PROD_ENV_FILE)

prod-up: prod-check
	docker compose --env-file $(PROD_ENV_FILE) $(PROD_COMPOSE_FILES) up -d --build

prod-down:
	docker compose --env-file $(PROD_ENV_FILE) $(PROD_COMPOSE_FILES) down

# 数据库备份 / 恢复（容器内 pg_dump -Fc + docker cp，二进制安全）
db-backup:
	powershell -ExecutionPolicy Bypass -File tools/db-backup.ps1 -OutDir backups

db-restore:
	powershell -ExecutionPolicy Bypass -File tools/db-restore.ps1 -File $(FILE)
