# WineTone data + ML pipeline.

VENV ?= .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.DEFAULT_GOAL := help

.PHONY: help venv install dev test lint pull-tier-a inspect status clean

help:
	@echo "WineTone — Makefile targets"
	@echo ""
	@echo "  Setup:"
	@echo "    make venv          create .venv"
	@echo "    make install       install the package + runtime deps"
	@echo "    make dev           install with dev extras (pytest, ruff, mypy)"
	@echo ""
	@echo "  Data pipeline:"
	@echo "    make pull-tier-a   pull every Tier A source (UCI x2 + WineEnthusiast)"
	@echo "    make status        show what's staged on disk"
	@echo "    make inspect S=<src>  show a staged source's schema + head()"
	@echo ""
	@echo "  Quality:"
	@echo "    make test          pytest"
	@echo "    make lint          ruff check"
	@echo ""
	@echo "  Housekeeping:"
	@echo "    make clean         remove __pycache__ and .pytest_cache"

venv:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip

install: venv
	$(PIP) install -e .

dev: venv
	$(PIP) install -e ".[dev]"

pull-tier-a:
	$(VENV)/bin/winetone pull --tier a

status:
	$(VENV)/bin/winetone status

inspect:
	$(VENV)/bin/winetone inspect $(S)

test:
	$(VENV)/bin/pytest

lint:
	$(VENV)/bin/ruff check src tests

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .mypy_cache
