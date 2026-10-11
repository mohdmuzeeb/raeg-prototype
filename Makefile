# All Docker calls use the colima context without changing the user's global docker context.
export DOCKER_CONTEXT ?= colima
COMPOSE := docker compose -f docker-compose.yml

.PHONY: lock up up-test down clean test-unit test-conformance test

lock:            ## re-lock Python deps and export hashed requirements for images
	uv lock
	uv export --frozen --no-dev --no-emit-project --no-header -o docker/requirements.lock

up:              ## start databases and provider (experiment mode: reset disabled)
	$(COMPOSE) up -d --build --wait

up-test:         ## start the stack with the test-only reset endpoint enabled
	PROVIDER_ALLOW_RESET=1 $(COMPOSE) up -d --build --wait

down:
	$(COMPOSE) down

clean:           ## remove containers AND volumes (fresh state)
	$(COMPOSE) down -v

test-unit:       ## unit tests; logs saved under results/tests/
	uv run python scripts/run_tests.py unit -- tests/unit

test-conformance: up-test  ## provider contract conformance suite against the running stack
	uv run python scripts/run_tests.py conformance --compose-logs provider -- tests/conformance

test: test-unit test-conformance
