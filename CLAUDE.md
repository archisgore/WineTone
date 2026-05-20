# CLAUDE.md — WineTone repo orientation

*For future Claude sessions landing in this repo. Read this once,
then drill into whichever artifact is relevant to the user's
current request.*

---

## What WineTone is

A **Pantone-like objective reference system for wine**: every
bottle gets a high-dimensional embedding (the *WinePrint*), every
user gets a calibrated personal projection (the *PalatePrint*),
and matching them is reduced to nearest-neighbor search.

The deeper thesis Archis articulated — and the framing to lean on
for any blog or paper writing — is in
`/Users/archisgore/.claude/projects/-Users-archisgore-github/memory/project_winetone_user_vocabulary_thesis.md`:
*recommender systems pick up word context via attention; this
project picks up the **user's personal context on how they use
words**.* "Grippy" for one user lives in Nebbiolo territory; for
another it might mean astringency-plus-alcohol. WineTone learns
that mapping per user.

The concept document is `PLAN.md`. The implementation plan is
`docs/DATA-AND-ML-PIPELINE-PLAN.md` — when in doubt about what
should exist, that document is the source of truth.

## Status (2026-05-19)

End-to-end pipeline working in PoC form. The recommend command
returns personalized results that demonstrably shift after
calibration. Specifically:

- **164,069 canonical wines** in CedarDB from ~287K source rows
  (WineEnthusiast 130k + 150k, UCI Wine Quality + UCI Wine,
  Wikidata SPARQL).
- **Dense embeddings**: 20,000 stratified-sampled wines, bge-small
  via fastembed/ONNX, 384-dim, stored as pgvector.
- **Sparse embeddings**: TF-IDF (1+2-grams, 50K vocab) over the
  full 164K corpus, joblib-serialized.
- **Personalization**: closed-form ridge or gradient descent
  (PyTorch + MLX, auto-detected backend). Versioned history table.
- **End-to-end demo**: `scripts/demo_calibrate_and_recommend.sh`
  loads 6 labels for sample user "archis", fits, runs three
  recommend queries. Same prompt before / after calibration
  shifts the results from generic US Riesling to Italian Nebbiolo.

What's *not* done: full-corpus dense embeddings (only 20K of
164K), TTB COLA scraper (deferred to Sprint 3 — design doc at
`docs/SCRAPER-PLAN-TTB.md`), fusion encoder / fine-tuning, multi-
language support. None of these block the current demo.

---

## Repo layout

```
WineTone/
├── README.md                     elevator pitch + how-to-run
├── PLAN.md                       v0.1 product concept
├── CLAUDE.md                     ← you are here
├── CONTRIBUTING.md
├── LICENSE                       Apache-2.0
├── Makefile                      every command you'd actually run
├── pyproject.toml                deps + optional `[mac]` extra for MLX
├── docker-compose.yml            CedarDB container (Postgres-wire)
├── docs/
│   ├── DATA-AND-ML-PIPELINE-PLAN.md   the actual build plan (read this!)
│   ├── PROGRESS.md                    running log per sprint
│   └── SCRAPER-PLAN-TTB.md            deferred Sprint 3 scraper design
├── scripts/
│   └── demo_calibrate_and_recommend.sh   end-to-end demo
├── src/winetone/                 all source code
│   ├── __init__.py
│   ├── paths.py                  data/raw, data/staging, data/canonical
│   ├── db.py                     SQLAlchemy + psycopg engine factory
│   ├── cli.py                    `winetone ...` Click commands
│   ├── canonicalize.py           Phase 2 — entity resolution
│   ├── embed.py                  Phase 3 — dense embeddings (fastembed)
│   ├── embed_sparse.py           Phase 3b — TF-IDF sparse
│   ├── recommend.py              Phase 4 — users, labels, ridge ridge-fit, hybrid retrieve
│   ├── calibrate.py              Phase 4 — gradient-descent fit (MLX / PyTorch)
│   ├── cluster.py                Phase 5 — KMeans over embeddings
│   └── sources/                  Phase 1 — data acquisition
│       ├── base.py                  Source ABC + http_get with retries
│       ├── uci_wine_quality.py
│       ├── uci_wine.py
│       ├── wine_enthusiast.py       130k v2
│       ├── wine_enthusiast_150k.py  v1 corpus
│       └── wikidata.py              SPARQL via query.wikidata.org
├── tests/
│   └── test_registry.py          sanity tests for source registry
└── data/                         gitignored — see paths.py
    ├── raw/<source>/<date>/         append-only verbatim payloads
    ├── staging/<source>/            cleaned Parquet
    └── canonical/sparse/            sparse matrix + vectorizer (joblib)
```

---

## The pipeline, in order

| Phase | Output | Command | Notes |
|---|---|---|---|
| 1 — Acquisition | Parquet per source in `data/staging/` | `make pull-tier-a` (free corpora) + `make pull-tier-b` (Wikidata) | ~287K rows total |
| 2 — Canonicalize | `wines`, `source_records`, `wine_features` in CedarDB | `make build-canonical` | 164,069 distinct wines via (producer, wine, vintage) dedup |
| 3 — Dense embed | `wine_embeddings` pgvector(384) in CedarDB | `make build-embeddings-sample` (20K) or `make build-embeddings` (full 164K — slow) | bge-small-en-v1.5 via fastembed/ONNX |
| 3b — Sparse embed | `data/canonical/sparse/*.joblib` + `wine_sparse_index` in CedarDB | `make build-sparse` | Full 164K, TF-IDF 1+2-gram, ~15s |
| 5 — Cluster | `wine_clusters`, `wine_cluster_centroids` in CedarDB | `make build-clusters` (default k=16) | KMeans for human exploration |
| 4 — Personalize | `user_labels` (append), `user_projections` (live), `user_calibration_history` (versioned) | `winetone calibrate add` / `... fit` | Auto-detects MLX > PyTorch CUDA > MPS > CPU |
| 4 — Recommend | top-k wines printed | `winetone recommend "free text"` | Hybrid α·dense + (1-α)·sparse, α defaults to 0.6 |

**Bootstrap from a fresh checkout:**
```bash
make dev-mac            # or `make dev` on non-Apple-Silicon
make pull-tier-a
make db-up-bg           # CedarDB on :5432 via docker compose
make build-canonical
make build-embeddings-sample
make build-sparse
make build-clusters
bash scripts/demo_calibrate_and_recommend.sh
```

---

## Key engineering decisions / gotchas

These are the lessons that cost a debug cycle each. If you find
yourself about to re-do one, look here first.

### CedarDB

- **`CREATE TABLE IF NOT EXISTS ... DEFAULT NOW()` crashed CedarDB
  v2026-05-18** (libc segfault + WAL recovery). All user-related
  tables are now created via:
  1. SELECT `information_schema.tables` to check existence
  2. Plain `CREATE TABLE` (no `IF NOT EXISTS`) if missing
  3. Application-supplied timestamps (no `DEFAULT NOW()`)
  Pattern lives in `recommend.init_user_schema()` and
  `calibrate.init_calibration_schema()`. Don't revert it.
- **`CREATE INDEX` requires AUTOCOMMIT isolation, not an explicit
  transaction.** See `canonicalize.py::_persist_cedardb` —
  `db.engine().execution_options(isolation_level="AUTOCOMMIT")` is
  the pattern. CedarDB will reject CREATE INDEX inside `engine.begin()`.
- **CedarDB doesn't accept `IF NOT EXISTS` on CREATE INDEX either.**
  Each index runs in its own autocommit connection so duplicate-name
  errors don't cascade.

### Python / ML

- **fastembed throughput on CPU is ~17 docs/sec for bge-small with
  concatenated review text.** Full 164K corpus ≈ 2.7 hours. We
  default to a 20K stratified sample for the PoC
  (`--sample 20000`). Multi-review wines preferred in the sample
  via `n_reviews >= 2` stratification.
- **pandas 3.0 changed `read_parquet(columns=[])` semantics** — it
  now returns an empty DataFrame. `cli.py::status` reads row counts
  via `pyarrow.parquet.ParquetFile(path).metadata.num_rows`.
- **`%px` in printk has no `0x` prefix** (irrelevant here but
  documented in the `04-gotchas.md` of `evasive-linux` if you ever
  see kernel addresses logged for module params).

### Backend abstraction

- `calibrate.detect_backend()` returns one of:
  `mlx`, `torch-cuda`, `torch-mps`, `torch-cpu`. Order of
  preference is MLX > CUDA > MPS > CPU.
- MLX is a Mac-only optional dep — install via `make dev-mac` or
  `pip install -e ".[mac]"`. On Linux + NVIDIA, MLX is skipped and
  PyTorch-CUDA wins.
- Mathematically all backends compute the same objective; only
  wall-clock cost differs.

---

## How user calibration flows

1. User says `winetone calibrate add -u archis -q "barolo" -d "tar
   and roses, grippy" --pick 0`. Row appended to `user_labels`.
2. After 5+ labels: `winetone calibrate fit -u archis`. Loads
   labels + wine embeddings, runs ridge regression OR
   PyTorch/MLX gradient descent (per `--backend`). Writes:
   - `user_projections` (current, replaces any previous)
   - `user_calibration_history` (versioned, append-only)
3. `winetone recommend "..." -u archis`. Encoder embeds the query,
   user's `A · L + b` projects it into wine space, hybrid scoring
   ranks. If no user given, runs the identity projection (generic).

**Feedback loop into the global corpus**: every canonical rebuild
(`make build-canonical`) reads `user_labels` and merges accumulated
user descriptions into `review_text_all`. The next embedding build
sees them. As users keep labeling, the global dataset grows with
their vocabulary.

---

## CLI commands you'll actually use

```
winetone list                       # show registered data sources
winetone pull --tier a              # download Tier A sources
winetone pull --tier b              # Wikidata SPARQL
winetone inspect <source>           # head() a staged source
winetone status                     # what's in data/staging/
winetone db-status                  # what's in CedarDB

winetone build canonical            # Phase 2
winetone build embeddings           # Phase 3 (full corpus — slow)
winetone build embeddings --sample 20000   # Phase 3 (fast)
winetone build sparse               # Phase 3b
winetone build clusters [-k N]      # Phase 5
winetone build all                  # canonical → embeddings → clusters

winetone calibrate backend          # show the auto-selected ML backend
winetone calibrate add -u <user> -q <query> -d <description> [--pick N]
winetone calibrate labels -u <user>
winetone calibrate fit -u <user> [--backend mlx|torch-cuda|torch-mps|torch-cpu|ridge]
winetone calibrate history -u <user>

winetone recommend "<free text query>" [-u <user>] [-k N] [--alpha 0.6] [--country X] [--variety Y]
winetone clusters [-k 16] [--examples 3]
```

---

## What NOT to do

- **Don't re-introduce `CREATE TABLE IF NOT EXISTS` in CedarDB.**
  It crashed the database; fix described above.
- **Don't try the full-corpus dense embedding without a tea kettle.**
  17 docs/sec × 164,069 = 2.7 hours of wall clock. Use
  `--sample 20000` for development cycles.
- **Don't `pip install sentence-transformers` or `transformers`
  for the encoder.** We deliberately chose fastembed (ONNX
  runtime, ~50MB install) over PyTorch sentence-transformers
  (~800MB install via torch CPU wheels). Same `bge-small-en-v1.5`
  model under the hood.
- **Don't store secrets in `~/.claude/settings.json`** (already
  noted as a hard rule for Archis's setup). The CedarDB password
  in `docker-compose.yml` is a local-dev value; that's fine.

---

## Where context lives

| Question | Look at |
|---|---|
| What is WineTone? | `PLAN.md`, then `README.md` |
| How was it supposed to be built? | `docs/DATA-AND-ML-PIPELINE-PLAN.md` |
| What's been built so far? | `docs/PROGRESS.md` (sprint-by-sprint log) |
| The user-vocabulary thesis | `~/.claude/projects/-Users-archisgore-github/memory/project_winetone_user_vocabulary_thesis.md` |
| TTB COLA scraper (deferred Sprint 3) | `docs/SCRAPER-PLAN-TTB.md` |
| What can run? | `Makefile` — every meaningful command is a target |
| What's in CedarDB right now? | `winetone db-status` |
| What backend would MLX/PyTorch pick? | `winetone calibrate backend` |

