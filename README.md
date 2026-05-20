# WineTone

> *Pantone for wine — calibrated to your vocabulary.*

A wine recommender that learns how **you** use words.

Modern recommendation models have figured out that *words have
context* — attention picks that up well. What they haven't
figured out is that **users have personal context on how they
use words**. "Oaky" from a Burgundy sommelier and "oaky" from
someone who grew up in India and reaches for sandalwood and
tandoor smoke when describing the same molecule are not the
same query. WineTone calibrates that mapping per user, so your
*intent* is what gets searched, not the literal token you typed.

## How it works in one line

You label 5–13 wines you know well, in your own words. WineTone
fits an `A·L + b` projection from your language-space into the
global wine-embedding space. Future queries from you are
projected through `A` before the nearest-neighbor search runs.

## Try it in five minutes (skip the 3-hour build)

The repo ships with a published release that contains the full
164K-wine canonical store, dense + sparse embeddings, and
KMeans clusters — same state the author's machine sits in. You
clone, download, import, and you're labeling within minutes.

```bash
git clone https://github.com/archisgore/WineTone
cd WineTone
make dev-mac                       # or `make dev` on Linux/Windows
make db-up-bg                      # CedarDB on :5432 in Docker

# Download prebuilt artifacts (~450MB) — skips the 3-hour CPU build
gh release download --pattern '*.tar.gz' -R archisgore/WineTone
make import-release FILE=$(ls -1 winetone-data-*.tar.gz | head -1)
```

You're ready. From here you can either use the CLI or the web demo.

### Option 1 — web demo

```bash
make serve              # → http://127.0.0.1:8000
```

HTMX + FastAPI. Side-by-side generic-vs-personalized comparison
in the browser. Pick a user, label wines, watch the
recommendations shift in real time.

### Option 2 — CLI

```bash
# Generic search (no calibration)
winetone recommend "subtle balanced terroir-driven"
winetone recommend "easy drinking light white"
winetone recommend "bold tannic California Cabernet"
```

## Calibrate to *your* vocabulary

Pick 5–13 wines you know well. Describe each one **in your own
words** — not standard tasting-note vocabulary. The more
idiosyncratic, the better. Define your terms against other
usages where it matters (`buttery — but theatre popcorn butter,
not the sour-yogurt California kind`).

```bash
# Find a wine and label it in one shot
winetone calibrate add -u alice \
  -q "Biondi Santi Brunello" --pick 0 \
  -d "extreme subtlety. All flavors balanced like Baroque music
      where every instrument has its place. I taste the soil, the
      grape, the region."

winetone calibrate add -u alice \
  -q "Tokaji 6 Puttonyos Aszú" --pick 0 \
  -d "sunshine in a bottle. Savoury, sweet, lasting on my tongue
      for hours. Like a fragrance in a glass."

# ... repeat for 5–13 wines, mixing styles you like AND dislike
# (negative labels matter as much as positive ones)

winetone calibrate labels -u alice    # review what you've labeled
```

When you've got at least 5 labels covering enough variety, fit:

```bash
winetone calibrate fit -u alice
```

The fit auto-detects the best ML backend: **MLX** on Apple
Silicon → **CUDA** on Linux+NVIDIA → **MPS** → **CPU**. A
13-label fit on Apple Silicon takes ~2 seconds.

## Search by your vocabulary

```bash
# Same query, different projection
winetone recommend "sunshine in a bottle"             # generic — random sparkling
winetone recommend "sunshine in a bottle" -u alice    # → Tokaj Aszú (if alice labeled it that way)

winetone recommend "volcanic minerals perfume"        # generic — Mosel Riesling (matches "minerals")
winetone recommend "volcanic minerals perfume" -u alice   # → Santorini whites (if alice used those words for Nykteri)
```

Phrases that mean **nothing** in the generic embedding will mean
exactly what **you** mean by them once the calibration runs.

You can iterate: add more labels, refit. Calibration history is
versioned in `user_calibration_history`, so you can see how your
vocabulary drift affects recommendations over time:

```bash
winetone calibrate history -u alice
```

## Architecture (one page)

| Phase | What | Storage |
|---|---|---|
| 1. Acquisition | Pull WineEnthusiast (130k + 150k), UCI Wine Quality / Wine, Wikidata SPARQL | `data/staging/*.parquet` (~287K rows) |
| 2. Canonicalize | Resolve `(producer, wine, vintage)` tuples to UUIDv5 | CedarDB: `wines`, `wine_features` (164,069 distinct) |
| 3a. Dense embed | `bge-small-en-v1.5` via fastembed/ONNX, GPU auto-detected | CedarDB pgvector(384) |
| 3b. Sparse embed | TF-IDF 1+2-gram, 50K vocab | `data/canonical/sparse/*.joblib` |
| 4. Personalize | Ridge regression OR PyTorch/MLX gradient descent | `user_projections` (live), `user_calibration_history` (versioned) |
| 5. Cluster | KMeans k=16 for human exploration | `wine_clusters`, `wine_cluster_centroids` |

**Hybrid scoring:** `α · dense_cosine + (1−α) · sparse_cosine`,
α defaults to 0.6.

**Personalization mechanism:** each label
`(wine_id, your_description)` becomes a training example
`your_text_embedding → wine_embedding`. The fit minimizes
`‖A·L + b − wine_emb‖² + λ_A·‖A − I‖² + λ_B·‖b‖²` — the prior
that keeps `A` near identity is what lets you train usefully on
just 5–13 examples instead of needing thousands.

## What WineTone is *not*

- **Not a wine review app.** Vivino exists.
- **Not a recommendation engine over star ratings.** Star ratings
  are aggregated subjective experience. We want the vocabulary
  underneath.
- **Not a marketing-copy generator.** The point is the inverse —
  *normalize* sensory language so "dry" and "buttery" and "oaky"
  stop meaning different things to different people.

## Building from source (if you don't want the prebuilt release)

```bash
make dev-mac                       # or `make dev`
make db-up-bg
make pull-tier-a                   # ~287K rows from public corpora
make build-canonical               # → 164K canonical wines
make build-embeddings-sample       # 20K-stratified-sample (fast); or `make build-embeddings` for full 164K (~3 hours)
make build-sparse                  # ~15 seconds
make build-clusters
make serve
```

## Docs

- [`PLAN.md`](PLAN.md) — original product concept
- [`docs/DATA-AND-ML-PIPELINE-PLAN.md`](docs/DATA-AND-ML-PIPELINE-PLAN.md) — implementation plan
- [`docs/PROGRESS.md`](docs/PROGRESS.md) — sprint-by-sprint build log
- [`CLAUDE.md`](CLAUDE.md) — orientation for future AI assistants working in this repo
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — how to get involved

## Author

**Archis Gore** — concept author. Email: `me@archisgore.com`.

## License

Apache-2.0. See [`LICENSE`](LICENSE).
