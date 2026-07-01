# Kith Post dev loop. PYTHONPATH is cleared so a sourced ROS (or any system
# Python) can't leak packages/pytest-plugins into the venv.
export PYTHONPATH :=

.PHONY: install dev test lint fmt typecheck docker

install:        ## create .venv and install all deps (incl. dev)
	uv sync

dev:            ## run the app locally with hot reload (http://localhost:8000), dry-run send
	uv run uvicorn kith.web.app:app --reload

dev-self:       ## run locally, sending REAL email through Gmail but only to YOU (self-only)
	KITH_SEND_MODE=self-only uv run uvicorn kith.web.app:app --reload

dev-live:       ## run locally, sending REAL email to the actual recipients — careful
	KITH_SEND_MODE=live uv run uvicorn kith.web.app:app --reload

test:           ## run the test suite
	uv run pytest

lint:           ## ruff lint
	uv run ruff check .

fmt:            ## ruff auto-format
	uv run ruff format .

typecheck:      ## mypy on the package
	uv run mypy kith

docker:         ## build + run the container (http://localhost:8000)
	docker compose up --build
