SHELL := /bin/bash
PYTHON := .venv/bin/python

.PHONY: setup dev lint security test test-unit test-integration test-browser test-browser-real verify docker-up docker-down logs

setup:
	python3 -m venv .venv
	$(PYTHON) -m pip install --upgrade pip setuptools wheel
	$(PYTHON) -m pip install -e '.[test]'

dev:
	YTDLP_WEB_ENVIRONMENT=development YTDLP_WEB_DATA_DIR=./data $(PYTHON) -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

lint:
	$(PYTHON) -m ruff check app tests
	$(PYTHON) -m ruff format --check app tests

security:
	$(PYTHON) -m bandit -q -r app
	$(PYTHON) -m pip_audit

test:
	$(PYTHON) -m pytest -q

test-unit:
	$(PYTHON) -m pytest -q -m 'not integration'

test-integration:
	$(PYTHON) -m pytest -q -m integration

test-browser:
	./scripts/test-browser.sh

test-browser-real:
	./scripts/test-browser-real.sh

verify: lint security test
	node --check app/static/app.js

docker-up:
	docker compose -f compose.yml up --build -d

docker-down:
	docker compose -f compose.yml down

logs:
	docker compose -f compose.yml logs -f app
