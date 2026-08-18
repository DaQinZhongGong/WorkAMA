.PHONY: dev down logs contract-check docs-check test smoke perf-gate

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
