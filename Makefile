# Convenience targets. `make help` lists them.
.DEFAULT_GOAL := help
VENV := .venv
PY   := $(VENV)/bin/python

.PHONY: help venv test run discover docker clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

venv:  ## Create .venv and install dependencies
	python3 -m venv $(VENV)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt
	@echo "Done. Run 'make test' or 'make discover'."

test:  ## Run every test suite
	@$(PY) -m tests.test_sequence
	@$(PY) -m tests.test_bootstrap
	@$(PY) -m tests.test_lsdp
	@$(PY) -m tests.test_discovery
	@$(PY) -m tests.test_auth
	@$(PY) -m tests.test_subnet
	@$(PY) -m tests.test_single_room
	@$(PY) -m tests.test_doorbells
	@$(PY) -m tests.test_admin

run:  ## Run the service locally against config/config.yaml
	DOORBELL_CONFIG=config/config.yaml DOORBELL_CHIME_DIR=chimes $(PY) -m app

discover:  ## Find BluOS players on this network
	$(PY) -m tools.discover

docker:  ## Build and start the container
	docker compose up -d --build

clean:  ## Remove caches and the venv
	rm -rf $(VENV) .pytest_cache
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
