# All Docker calls use the colima context without changing the user's global docker context.
export DOCKER_CONTEXT ?= colima
COMPOSE := docker compose -f docker-compose.yml
ALL_PROFILES := --profile systems --profile temporal

.PHONY: lock up up-test down clean test-unit test-property test-conformance test-gateway test-integration test-systems up-systems run-experiment test-harness test

lock:            ## re-lock Python deps and export hashed requirements for images
	uv lock
	uv export --frozen --no-dev --no-emit-project --no-header -o docker/requirements.lock

up:              ## start databases and provider (experiment mode: reset disabled)
	$(COMPOSE) up -d --build --wait

up-test:         ## start the stack with the test-only reset endpoint enabled
	PROVIDER_ALLOW_RESET=1 $(COMPOSE) up -d --build --wait

down:
	$(COMPOSE) $(ALL_PROFILES) down

up-systems:      ## M5: also start Temporal, its worker, and the systems service (reset endpoint ON)
	PROVIDER_ALLOW_RESET=1 CODE_SHA=$$(git rev-parse HEAD) $(COMPOSE) $(ALL_PROFILES) up -d --build --wait

clean:           ## remove containers AND volumes (fresh state)
	$(COMPOSE) $(ALL_PROFILES) down -v

test-unit:       ## unit tests; logs saved under results/tests/
	uv run python scripts/run_tests.py unit -- tests/unit

test-conformance: up-test  ## provider contract conformance suite against the running stack
	uv run python scripts/run_tests.py conformance --compose-logs provider -- tests/conformance

test-property:   ## M4 model-based safety checks of the controller (no Docker)
	uv run python scripts/run_tests.py property -- tests/property

test-integration: up-test  ## M4 engine + store + provider: scenarios, real-process crashes, zombies, concurrency
	uv run python scripts/run_tests.py integration --compose-logs provider -- tests/integration

test-systems: up-systems  ## M5: F0 smoke + definition-conformance tests of every system via its HTTP contract
	uv run python scripts/run_tests.py systems --compose-logs systems temporal-worker -- tests/systems

run-experiment:  ## run an experiment config: make run-experiment EXP=config/experiments/m6-exit.yaml
	CODE_SHA=$$(git rev-parse HEAD) uv run python -m harness.runner $(EXP)

test-harness:    ## M6 harness unit tests (no Docker)
	uv run python scripts/run_tests.py harness -- tests/harness

test: test-unit test-property test-harness test-conformance test-gateway test-integration test-systems

test-gateway: up-test  ## M3 gateway store: transitions, concurrency, auditability, projection rebuild
	uv run python scripts/run_tests.py gateway --compose-logs raeg-db -- tests/gateway
