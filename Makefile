# WineTone data + ML pipeline.

VENV ?= .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.DEFAULT_GOAL := help

.PHONY: help venv install dev test lint pull-tier-a pull-tier-b inspect status clean \
        db-up db-up-bg db-down db-status build-canonical

help:
	@echo "WineTone — Makefile targets"
	@echo ""
	@echo "  Setup:"
	@echo "    make venv          create .venv"
	@echo "    make install       install the package + runtime deps"
	@echo "    make dev           install with dev extras (pytest, ruff, mypy)"
	@echo ""
	@echo "  Data pipeline:"
	@echo "    make pull-tier-a   pull every Tier A source (UCI x2 + 2x WineEnthusiast)"
	@echo "    make pull-tier-b   pull every Tier B source (Wikidata; TTB COLA in Sprint 3)"
	@echo "    make status        show what's staged on disk"
	@echo ""
	@echo "  Canonical store (CedarDB):"
	@echo "    make db-up         start CedarDB in foreground"
	@echo "    make db-up-bg      start CedarDB in background"
	@echo "    make db-down       stop CedarDB"
	@echo "    make db-status     check connection + canonical row counts"
	@echo "    make build-canonical  Phase 2: resolve canonical wines + features"
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

# Mac-only variant: also installs MLX for native Apple Silicon training.
dev-mac: venv
	$(PIP) install -e ".[dev,mac]"

pull-tier-a:
	$(VENV)/bin/winetone pull --tier a

pull-tier-b:
	$(VENV)/bin/winetone pull --tier b

status:
	$(VENV)/bin/winetone status

inspect:
	$(VENV)/bin/winetone inspect $(S)

test:
	$(VENV)/bin/pytest

lint:
	$(VENV)/bin/ruff check src tests

db-up:
	docker compose up

db-up-bg:
	docker compose up -d
	@echo "Waiting for CedarDB to accept connections..."
	@for i in 1 2 3 4 5 6 7 8 9 10; do \
	    $(VENV)/bin/winetone db-status >/dev/null 2>&1 && exit 0; \
	    sleep 1; \
	done; \
	echo "CedarDB did not come up within 10s; check 'docker compose logs cedardb'"; \
	exit 1

db-down:
	docker compose down

db-status:
	$(VENV)/bin/winetone db-status

build-canonical:
	$(VENV)/bin/winetone build canonical

build-embeddings:
	$(VENV)/bin/winetone build embeddings

# Faster PoC variant: encode only ~20k stratified wines instead of the full 164k.
build-embeddings-sample:
	$(VENV)/bin/winetone build embeddings --sample 20000

build-sparse:
	$(VENV)/bin/winetone build sparse

build-all:
	$(VENV)/bin/winetone build all

# Launch the local web demo at http://127.0.0.1:8000
serve:
	$(VENV)/bin/winetone serve

# Package trained artifacts for publishing as a GitHub release.
export-release:
	$(VENV)/bin/winetone export-release

# Import a downloaded release tarball. Usage: make import-release FILE=path
import-release:
	$(VENV)/bin/winetone import-release "$(FILE)"

# Fine-tune the encoder. Slow — runs locally against CedarDB + GPU/MPS.
# Workflow:
#   1. make dev-mac (or dev) + pip install -e ".[finetune]"
#   2. make db-up-bg && make build-canonical    (need source_records)
#   3. make fine-tune-encoder
#   4. After it finishes:
#       - point embed.py's MODEL_NAME at data/models/bge-small-winetone
#       - rerun `winetone build embeddings` to re-encode the corpus
#       - rerun `winetone build sparse` (sparse vocab stays the same;
#         but the wine_sparse_index reflects the new wine set)
#       - winetone export-release  →  upload new tarball to GitHub
#       - factory_reboot the Space to pick it up
fine-tune-encoder:
	$(PIP) install -e ".[finetune]"
	$(PY) scripts/fine_tune_encoder.py --epochs 1 --batch-size 32 \
	    --max-pairs 200000 --export-onnx

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .mypy_cache
