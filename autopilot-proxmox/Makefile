.PHONY: setup test test-unit test-compiler test-api lint clean help

VENV ?= .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

help: ## Show available targets
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-15s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

setup: ## Create venv and install dev deps
	python3 -m venv $(VENV)
	$(PIP) install -U pip
	$(PIP) install -r requirements-dev.txt

test: ## Run full test suite
	$(PY) -m pytest

test-unit: ## Unit tests only (no FastAPI integration)
	$(PY) -m pytest tests/test_crypto.py tests/test_sequences_db.py tests/test_ubuntu_compiler_*.py tests/test_ubuntu_enrollment.py

test-compiler: ## Ubuntu compiler tests
	$(PY) -m pytest tests/test_ubuntu_compiler_*.py tests/test_linuxesp_parity.py

test-api: ## API/integration tests
	$(PY) -m pytest tests/test_sequences_api.py tests/test_web.py tests/test_ubuntu_seed_iso.py

lint: ## Ansible syntax check
	$(VENV)/bin/ansible-playbook --syntax-check playbooks/*.yml

clean: ## Remove venv and pytest caches
	rm -rf $(VENV) .pytest_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
