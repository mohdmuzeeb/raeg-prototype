# All Docker calls use the colima context without changing the user's global docker context.
export DOCKER_CONTEXT ?= colima
COMPOSE := docker compose -f docker-compose.yml

.PHONY: lock up up-test down clean test-unit test-property test-conformance test-gateway test-integration test

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

test-property:   ## M4 model-based safety checks of the controller (no Docker)
	uv run python scripts/run_tests.py property -- tests/property

test-integration: up-test  ## M4 engine + store + provider: scenarios, real-process crashes, zombies, concurrency
	uv run python scripts/run_tests.py integration --compose-logs provider -- tests/integration

test: test-unit test-property test-conformance test-gateway test-integration

test-gateway: up-test  ## M3 gateway store: transitions, concurrency, auditability, projection rebuild
	uv run python scripts/run_tests.py gateway --compose-logs raeg-db -- tests/gateway
