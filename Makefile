# HLBot — developer workflow
#
# Quick reference:
#   make help         Show this message
#   make test         Run unit tests
#   make test-fast    Skip the slow integration tests
#   make test-cov     Run tests with coverage report
#   make lint         Run ruff + mypy
#   make backtest     Run walk-forward backtest
#   make calibrate    Run strategy calibration sweep
#   make smoke        Run exchange adapter smoke test
#   make report       Generate the hourly status report
#   make run          Start the live bot
#
# All targets use the project's Python (`python` on PATH or PYTHON env var).

PYTHON ?= python

.PHONY: help test test-fast test-cov lint backtest calibrate smoke report run clean

help:
	@echo "HLBot — common targets:"
	@echo "  test       — run the full test suite"
	@echo "  test-fast  — skip slow integration tests (run with -m 'not slow')"
	@echo "  test-cov   — run tests with coverage report"
	@echo "  lint       — run ruff and mypy"
	@echo "  backtest   — run walk-forward backtest"
	@echo "  calibrate  — run strategy calibration sweep"
	@echo "  smoke      — run exchange adapter smoke test"
	@echo "  run        — start the live bot (port 8000)"

test:
	$(PYTHON) -m pytest -v

test-fast:
	$(PYTHON) -m pytest -v -m "not slow"

test-cov:
	$(PYTHON) -m pytest -v --cov=src --cov-report=term-missing

lint:
	$(PYTHON) -m ruff check src tests
	$(PYTHON) -m mypy src

backtest:
	$(PYTHON) scripts/run_backtest.py

calibrate:
	$(PYTHON) scripts/run_calibration.py

smoke:
	$(PYTHON) scripts/test_adapter_smoke.py

report:
	$(PYTHON) scripts/hourly_report.py

run:
	$(PYTHON) -m uvicorn src.api.main:create_app --factory --host 0.0.0.0 --port 8000

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
	find . -type d -name __pycache__ -exec rm -rf {} +
