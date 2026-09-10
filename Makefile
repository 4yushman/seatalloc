# Automated Event Seat Allocation Engine — developer tasks
.PHONY: help install test test-fast test-all lint type check bench demo simulate clean fmt

PY ?= python3
export PYTHONPATH := src

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## Install the package with dev extras
	$(PY) -m pip install -e ".[dev]"

test:  ## Run the full test suite
	$(PY) -m pytest -q

test-fast:  ## Run everything except the empirical timing tests
	$(PY) -m pytest -q -m "not slow"

test-all: test  ## Alias for `make test`

lint:  ## Ruff lint
	$(PY) -m ruff check src tests

fmt:  ## Ruff format + autofix
	$(PY) -m ruff check --fix src tests && $(PY) -m ruff format src tests

type:  ## Mypy
	$(PY) -m mypy src/seatalloc

check: lint type test  ## Lint + types + tests

bench:  ## Regenerate docs/BENCHMARKS.md and benchmarks/results.json
	$(PY) -m seatalloc bench --out docs/BENCHMARKS.md

demo:  ## Run the guided demo (writes examples/demo/)
	$(PY) -m seatalloc demo --out examples/demo

simulate:  ## Run a 5k-registration simulation (writes data/sim/)
	$(PY) -m seatalloc simulate --attendees 5000 --capacity 1000 --cancel-rate 0.15 --out data/sim

clean:  ## Remove caches and generated output
	rm -rf .pytest_cache .ruff_cache .mypy_cache **/__pycache__ data/sim data/import out \
		dist build *.egg-info .hypothesis
