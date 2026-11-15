# Burn Rate: everyday commands. `make help` lists them.
# Deployment details: deploy/README.md.

PROFILE ?= small
export PROFILE

.DEFAULT_GOAL := help
.PHONY: help test test-api test-web lint dev build deploy smoke manifests validate compose-up compose-down

help:            ## this list
	@grep -E '^[a-z-]+: .*##' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  make %-14s %s\n", $$1, $$2}'

test: test-api test-web  ## all tests: Python (pytest, without the browser tests) and front end (vitest)

test-api:        ## Python tests only
	uv run pytest -m "not e2e"

test-web:        ## front-end tests only (skipped until web/node_modules exists: cd web && npm ci)
	@if [ -d web/node_modules ]; then cd web && npm test; else echo "web/node_modules not found: skipped (cd web && npm ci)"; fi

lint:            ## ruff for Python, tsc for the front end, bash -n / kustomize for deploy/
	uv run ruff check .
	@if [ -d web/node_modules ]; then cd web && npm run typecheck; else echo "web/node_modules not found: typecheck skipped"; fi
	deploy/scripts/validate.sh

dev:             ## the whole back end in one process on http://localhost:8000 (SQLite in .data/, no Kafka needed)
	BR_MODE=local BR_ENV=dev uv run python -m burnrate.run

build:           ## build both images for linux/amd64 and push them (tags: git short sha + latest)
	deploy/scripts/build.sh

deploy:          ## roll the last built tag out: schema job, apply, wait (PROFILE=small|full)
	deploy/scripts/deploy.sh

smoke:           ## check the public URL from outside
	deploy/scripts/smoke.sh

manifests:       ## regenerate deploy/apps/{small,full} from burnrate/services/__init__.py
	deploy/scripts/gen-apps.sh

validate:        ## everything under deploy/ that can be checked without a cluster (charts, CRD schemas, budget)
	deploy/scripts/validate.sh --all

compose-up:      ## PostgreSQL, Kafka, Valkey and the API image (BR_MODE=local) on localhost:8000
	docker compose up --build -d --wait
	@echo "http://localhost:8000/healthz"

compose-down:    ## stop the local stack and delete its volumes
	docker compose down -v
