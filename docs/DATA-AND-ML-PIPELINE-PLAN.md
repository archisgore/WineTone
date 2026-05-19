# Data & ML Pipeline Plan

*WineTone — Concept by Archis | v0.1 of the data/ML build plan*

A four-phase plan: (1) ingest every publicly available wine
dataset we can legally pull; (2) normalize it into one flat
table with a canonical `wine_id` per (producer, wine, vintage);
(3) train an embedding model that produces one high-dimensional
vector per wine; (4) ship a personalization layer that learns a
user's labeling style from 5 samples and projects future
descriptions through it.

> This document is operational, not conceptual. It calls out
> concrete data sources, schema decisions, model choices, and
> the unresolved questions you need to decide before building.

---

## Table of contents

1. [Phase 1 — Data acquisition](#phase-1--data-acquisition)
2. [Phase 2 — Normalization pipeline](#phase-2--normalization-pipeline)
3. [Phase 3 — Embedding model](#phase-3--embedding-model)
4. [Phase 4 — Personalized recommendations](#phase-4--personalized-recommendations)
5. [Cross-cutting concerns](#cross-cutting-concerns)
6. [Decisions you need to make](#decisions-you-need-to-make)

---

## Phase 1 — Data acquisition

### 1.1 Source inventory

We group sources by acquisition cost (engineering effort + legal
posture + ongoing maintenance), not by data volume.

#### Tier A — instant, legal, redistributable

| Source | What's in it | Volume | Format |
|---|---|---|---|
| [UCI Wine Quality](https://archive.ics.uci.edu/dataset/186/wine+quality) | physicochemical (pH, residual sugar, alcohol, etc.) + quality scores for red + white | ~6,500 samples | CSV |
| [UCI Wine](https://archive.ics.uci.edu/dataset/109/wine) | 13 chemical attributes, 3 cultivars from one Italian region | 178 samples | CSV |
| [WineEnthusiast 130k reviews (Kaggle)](https://www.kaggle.com/datasets/zynicide/wine-reviews) | sommelier reviews, scores, variety, region, price, designation | ~130,000 | CSV |
| [Wine Reviews — Tidy Tuesday](https://github.com/rfordatascience/tidytuesday/tree/main/data/2019/2019-05-28) | reformatted slice of WineEnthusiast | ~130,000 | CSV |
| [Pinot Noir Aromatic Quality](https://data.mendeley.com/datasets/various) | aroma compound concentrations for Pinot Noir | thousands | XLSX |
| Various Kaggle wine datasets | mixed | thousands | mixed |

**Acquisition mechanics:** direct download. Day-one effort.
Apache-2.0-compatible licenses or public domain throughout.

#### Tier B — public, scrapable, ToS-compliant with care

| Source | What's in it | Approach | Legal posture |
|---|---|---|---|
| [TTB COLA database](https://www.ttbonline.gov/colasonline/publicSearchColasBasic.do) | every US-sold wine label (producer, brand, vintage, varietal, ABV, label image, approval date) | API + scrape | US federal data; public record |
| [EU PDO/PGI registry (eAmbrosia)](https://ec.europa.eu/agriculture/eambrosia/geographical-indications-register/) | every European geographical-indication wine | API | EU open data |
| [INAO open data (France)](https://www.inao.gouv.fr/Espace-presse/Donnees-ouvertes) | French AOC/IGP boundaries, varietals, vintages | bulk download | French government open data |
| [USDA Grape Variety Database](https://www.ars-grin.gov/) | grape varietal taxonomies | bulk download | US federal data |
| Wikipedia / Wikidata | producer pages, region pages, vintage charts | Wikidata SPARQL | CC-BY-SA |
| Crossref + Semantic Scholar | scientific papers on wine chemistry with supplementary data | API | open access |
| arXiv / bioRxiv | preprints with data tables | API | author-licensed (usually CC-BY) |

**Acquisition mechanics:** rate-limited scraping or API calls.
Several weeks of pipeline-building effort. Persist raw payloads
to S3 / object storage so we can re-parse without re-fetching.

#### Tier C — ambiguous ToS, requires judgment or partnership

| Source | What's in it | Approach | Posture |
|---|---|---|---|
| [CellarTracker](https://www.cellartracker.com/) | user tasting notes, scores, cellar inventory | their **affiliate API** (paid, limited) OR partnership | scraping is against ToS; the API is the right path |
| [Vivino](https://www.vivino.com/) | 50M+ user reviews, photos, prices | their **affiliate API** OR partnership only | scraping is explicitly prohibited; their API is the right path |
| [Wine.com / total wine catalog sites](https://www.wine.com/) | retail catalog with notes | crawl with `robots.txt` respect | per-site ToS check needed |
| [Wine-Searcher](https://www.wine-searcher.com/) | global price aggregation | paid API only | scraping ToS-prohibited |
| Producer technical sheets | chemistry for premium wines | targeted scrape of producer sites | usually allowed by `robots.txt`; respect per-site |
| [Decanter / Wine Spectator / Wine Advocate archives](https://www.decanter.com/) | professional reviews | paid subscriptions or partnerships | scraping not viable |

**Acquisition mechanics:** Either (a) pay for API access, (b)
strike partnerships, or (c) skip. The first two are recommended
over scraping; the third is acceptable for PoC since the Tier
A + B data is sufficient to bootstrap.

#### Tier D — commission new data

The single biggest data quality lever, and the most expensive item:

| Item | What it adds | Approximate cost |
|---|---|---|
| Commissioned **GC-MS analysis** at an academic lab | 200–400 volatile compounds per wine — the chemistry layer | $150–300 per wine |
| Commissioned **NMR profile** | geographic-provenance fingerprint | $200–500 per wine |
| Commissioned **physicochemical panel** | pH / acidity / sugar / SO₂ / phenolics | $50–100 per wine |

PoC scale: 20–30 wines through full GC-MS = $5k–9k. See
`PLAN.md` §"Budget Estimate".

### 1.2 Acquisition architecture

```
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│ Tier A       │  │ Tier B       │  │ Tier C       │
│ direct DL    │  │ scrapers     │  │ paid APIs    │
└──────┬───────┘  └──────┬───────┘  └──────┬───────┘
       │                  │                  │
       └─────────┬────────┴───────┬──────────┘
                 ▼                ▼
         ┌────────────────────────────────┐
         │ raw/  (object store, append-   │
         │   only, one folder per source) │
         │   * keep verbatim payloads     │
         │   * preserve fetch timestamps  │
         │   * never overwrite            │
         └────────────────┬───────────────┘
                          ▼
                  parsing/staging
                  (Phase 2)
```

**Storage choice:** S3 (or any object store: R2, GCS, MinIO). One
prefix per source. Every fetch writes a new versioned blob with a
content hash filename — never overwrite. This is your audit trail
and your "if-the-schema-changes-can-we-re-parse-without-re-fetching"
guarantee.

**Source rotation cadence:**

- Tier A: snapshot once, refresh quarterly (datasets rarely
  update).
- Tier B: incremental — TTB and EU registries are append-only; we
  pull deltas weekly. Scientific papers refresh monthly.
- Tier C: refresh per the API's rate limit.

### 1.3 Scrapers — concrete engineering

For Tier B, build a small uniform scraper framework. Each scraper
implements:

```python
class Scraper(Protocol):
    name: str                       # "ttb_cola", "eu_pdo", ...
    def discover(self) -> Iterator[Ref]: ...     # what to fetch
    def fetch(self, ref: Ref) -> bytes: ...      # do the fetch
    def parse(self, payload: bytes) -> RawRecord: ...
```

Built on:
- `httpx` for async + rate-limited HTTP
- `tenacity` for retries
- `prefect` or `dagster` for orchestration (or just a Makefile + cron for PoC)
- Output: Parquet files in `raw/<source>/YYYY-MM-DD/*.parquet`

Per-source `robots.txt` compliance is checked at startup; the
scraper refuses to run for any URL that's disallowed.

### 1.4 Legal stance

Three rules of thumb:

1. **`robots.txt` is the floor, not the ceiling.** If `robots.txt`
   says no, we don't crawl. If it says yes, we still check the
   site's ToS for ambiguous language.
2. **Public records are public.** TTB COLA, EU registries,
   government data — these are unambiguously redistributable.
3. **User-generated content needs a license.** Reviews on Vivino,
   CellarTracker, or Reddit are individually copyrightable by the
   author. The site's ToS governs aggregate use. We respect that
   — either via the site's official API (which grants the license
   we need) or by skipping.

For redistribution: any dataset we **ourselves publish** must be
either (a) entirely from Tier A + Tier B + commissioned Tier D
sources, or (b) sourced from a partner who's granted us a
redistribution license.

---

## Phase 2 — Normalization pipeline

### 2.1 The central problem: entity resolution

The same wine appears as different strings across sources:

| Source | String |
|---|---|
| WineEnthusiast | `Château Margaux 2018` |
| CellarTracker | `Margaux 2018, Ch.` |
| TTB COLA | `CHATEAU MARGAUX 2018 PREMIER GRAND CRU CLASSE` |
| Vivino | `Chateau Margaux 2018` |
| Producer site | `Premier Grand Cru Classé · Margaux · 2018` |

We need to collapse all of these to one canonical record.

### 2.2 Canonical wine identity

The canonical wine is the **(producer, wine, vintage)** triple,
plus a stable identifier:

```sql
CREATE TABLE wines (
  wine_id           UUID PRIMARY KEY,
  producer_id       UUID NOT NULL REFERENCES producers(producer_id),
  wine_name         TEXT NOT NULL,             -- canonical name
  vintage           SMALLINT,                  -- NULL for NV
  variety           TEXT[],                    -- normalized varietal taxonomy
  region            UUID REFERENCES regions(region_id),
  appellation       TEXT,
  wine_type         wine_type_enum,            -- red/white/rosé/sparkling/fortified/dessert/orange
  classification    TEXT,                      -- "Premier Grand Cru Classé", "Grand Cru", ...
  created_at        TIMESTAMP NOT NULL,
  canonical_form    TSVECTOR                   -- full-text search index
);
CREATE UNIQUE INDEX ON wines (producer_id, wine_name, vintage);
```

Plus auxiliary tables: `producers`, `regions`, `varieties`,
`source_records` (per-source raw mappings), `match_decisions`
(audit log of which records were merged into which canonical
wine).

### 2.3 Entity resolution strategy

Four stages, increasing cost per record:

#### Stage 1 — canonicalization (fast, deterministic)

Per source-record, compute a canonical form:

```python
def canonicalize(raw: RawRecord) -> CanonicalForm:
    return CanonicalForm(
        producer = strip_accents(lower(strip_titles(raw.producer))),
        # "Château" → "chateau"; "Domaine de la Romanée-Conti" → "romanee conti"
        wine     = strip_accents(lower(strip_classifications(raw.wine))),
        # drop "Premier Grand Cru Classé", "Riserva", "Reserve" — those go to a separate field
        vintage  = parse_vintage(raw.vintage_text),
        # "NV", "Multi-Vintage" → None
    )
```

After stage 1, exact-match on the canonical triple resolves
roughly 70% of records.

#### Stage 2 — fuzzy match (medium cost)

For the remaining 30%, use string similarity (RapidFuzz or
similar) + small sentence-transformer embeddings.

- Per source-record, compute a 384-dim embedding of the
  canonical-form-as-string using `BAAI/bge-small-en-v1.5`.
- For each remaining candidate, find nearest neighbors among
  existing canonical records via pgvector.
- Auto-accept matches above a threshold (e.g. cosine > 0.95);
  send 0.80–0.95 to a review queue.

#### Stage 3 — ML classifier (high cost)

Train a small binary classifier:

```python
def is_same_wine(record_a: RawRecord, record_b: RawRecord) -> float:
    # Features: producer-name fuzzy ratio, wine-name fuzzy ratio,
    # vintage match (exact / off-by-one / mismatch), variety overlap,
    # region match, classification compatibility, alcohol % delta, ...
    return p(same_wine | features)
```

Train on the auto-accepted + manual-review labels from stage 2.
Use it to clear the 0.50–0.80 fuzzy-similarity band.

#### Stage 4 — manual review (highest cost, lowest volume)

For ~1% of records (rare wines, ambiguous producers, missing
vintages), present a side-by-side UI and accept human merge
decisions. Log everything in `match_decisions` so future runs of
stages 1–3 can learn from past judgments.

**Expected outcome on PoC corpus** (~150k WineEnthusiast +
500k TTB + smaller sources): ~600k–800k distinct wines.

### 2.4 Schema unification — the flat table

Once wines are canonically identified, every signal collapses
into one wide row. Some columns are scalars, some are arrays
(multiple reviews, multiple chemical measurements):

```sql
CREATE TABLE wine_features (
  wine_id          UUID PRIMARY KEY REFERENCES wines(wine_id),

  -- structured attributes (after normalization)
  alcohol_pct      REAL,
  ph               REAL,
  residual_sugar_gl REAL,
  titratable_acidity_gl REAL,
  free_so2_mgl     REAL,
  total_so2_mgl    REAL,
  total_phenolics  REAL,
  anthocyanins     REAL,
  tannin_polym_idx REAL,
  color_lab_l      REAL,                  -- CIE Lab*
  color_lab_a      REAL,
  color_lab_b      REAL,

  -- aggregated reviews
  review_text_all       TEXT[],           -- concatenated for embeddings
  review_text_count     INT,
  score_we_100          INT,              -- WineEnthusiast 0-100
  score_rp_100          INT,              -- Robert Parker if available
  score_cellar_tracker  REAL,             -- 0-100, averaged
  score_vivino          REAL,             -- 0-5, averaged

  -- structured taxonomies (decoded from reviews/labels)
  primary_aromas       TEXT[],           -- ["cherry", "tar", "rose"]
  secondary_aromas     TEXT[],
  tertiary_aromas      TEXT[],
  sweetness_label      sweetness_enum,
  acidity_label        acidity_enum,
  tannin_label         tannin_enum,
  body_label           body_enum,
  finish_length        finish_enum,

  -- pricing (multi-source, store time-series elsewhere)
  median_price_usd     REAL,
  drink_window_start   SMALLINT,
  drink_window_end     SMALLINT,

  -- raw GC-MS feature (when available)
  gcms_vector          REAL[],           -- 200–400 dims, mostly NULL
  gcms_compound_map    JSONB,            -- compound → concentration
  has_gcms             BOOLEAN DEFAULT FALSE,

  last_refreshed       TIMESTAMP NOT NULL
);
```

**Why one wide flat table** instead of normalized 3NF: the
downstream consumer is an embedding model that needs to read all
features per wine in one shot. The data warehouse pattern wins
here over the OLTP pattern.

### 2.5 Pipeline orchestration

```
┌─────────────────────┐
│ raw/<source>/...    │  (Phase 1 output)
└──────────┬──────────┘
           ▼
   per-source parsers
   (clean + cast + reject obviously-bad rows)
           ▼
┌─────────────────────┐
│ stage/<source>/...  │  (Parquet, typed)
└──────────┬──────────┘
           ▼
    entity resolution
    (Stages 1 → 4 above)
           ▼
┌─────────────────────┐
│ wines, producers,    │  (Postgres, canonical)
│ regions, varieties   │
│ source_records       │  (raw → canonical mapping)
│ match_decisions      │  (audit log)
└──────────┬──────────┘
           ▼
     feature consolidation
     (rollup per wine)
           ▼
┌─────────────────────┐
│ wine_features       │  (Postgres, flat)
└─────────────────────┘
           ▼
    (Phase 3: embeddings)
```

**Tooling:** dbt for the SQL transforms, Prefect or Dagster for
the orchestration, Great Expectations for data quality assertions
(non-null counts, reasonable value ranges, etc.). For PoC, all of
this can be `Makefile` + `python -m winetone.pipelines.X` — no
orchestration platform required until we hit weekly schedules.

### 2.6 Data quality assertions

Every stage emits a quality report. Block downstream stages on:

- Stage 1: ≥ 90% of records produce a non-null canonical form.
- Stage 2: ≥ 95% of records resolve to a wine_id.
- Stage 3 (wine_features): ≥ 80% of wines have at least one
  review and one structured attribute; ≥ 10 wines have GC-MS
  (once commissioned).

If a stage fails its assertions, the pipeline halts and a human
investigates.

---

## Phase 3 — Embedding model

### 3.1 Design goals

A single high-dimensional vector per wine such that:

- **Same-wine distance is small.** Different reviews of Château
  Margaux 2018 should land close together.
- **Within-region similarity is captured.** Different Burgundy
  Pinot Noirs cluster more than a Burgundy and an Oregon Pinot.
- **Style is captured beyond region.** A new-world Riesling and an
  old-world Riesling are closer than a new-world Riesling and a
  new-world Cabernet — even though the regions differ.
- **The space is dense and continuous.** Interpolating between
  two wines should produce something semantically meaningful.

### 3.2 Input modalities

Per wine, we have up to four input streams:

| Modality | Source | Coverage | Typical dim |
|---|---|---|---|
| **Text reviews** | WineEnthusiast, CellarTracker, etc. | high (most wines have ≥1 review) | variable text → 384–1024 dim sentence embedding |
| **Structured attributes** | aggregated from all sources | high | ~30 dim numerical + categorical |
| **Chemical (GC-MS)** | commissioned + scientific papers | low (~hundreds of wines initially) | 200–400 dim sparse vector |
| **Tasting taxonomy** | decoded from reviews (Phase 2 §2.4) | medium | ~100 dim multi-hot |

### 3.3 Architecture choice — recommendation

A **multi-modal joint encoder with contrastive training**:

```
text reviews ──→ sentence-transformer (frozen or fine-tuned)
                     ↓
                  text vector (768)
                     ↓
structured ──→ MLP encoder → struct vector (128)
                     ↓
chem ──→ chemical encoder ─→ chem vector (128) (zero-vector when absent)
                     ↓
                  concat (1024)
                     ↓
                  fusion MLP
                     ↓
                  wine embedding (256)
```

**Why this stack:**

- Text is the densest signal; sentence-transformers (e.g.
  `BAAI/bge-large-en-v1.5`, `intfloat/e5-large`) get you 80% of
  the way for free.
- Structured features are low-dim but high-signal — region,
  variety, vintage matter a lot.
- Chemical features are sparse but extremely informative when
  present. Handle absence by passing a zero vector + a presence
  bit; the fusion MLP learns to discount when absent.
- Fusion MLP is small (one or two hidden layers, 512 → 256 →
  256-dim output).

### 3.4 Training procedure

#### Stage A: pre-train on text alone

- Take the 130k WineEnthusiast reviews + any other open review
  corpora.
- Fine-tune the sentence-transformer using contrastive learning:
  same-wine pairs are positives, random different-wine pairs are
  negatives.
- This bakes "wine-domain knowledge" into the text encoder
  without needing the full multi-modal stack.
- Output: a `bge-wine` model checkpoint.

#### Stage B: train fusion encoder with multi-modal contrastive

- Freeze the text encoder.
- For each wine with ≥ 2 source-records, treat the records as
  positive pairs after passing through the full stack.
- For each batch, draw in-batch negatives from other wines.
- Loss: InfoNCE / NT-Xent with temperature τ ≈ 0.07.

```python
loss = -log(
    exp(sim(z_i, z_i_positive) / τ) /
    Σ_j exp(sim(z_i, z_j) / τ)
)
```

- Train for ~10 epochs, batch size 256, early-stop on validation
  retrieval accuracy.

#### Stage C (optional): fine-tune with rated triplets

If we have professional taster panels labeling triplets ("A is
more similar to B than to C"), use triplet loss to refine. This
is a Phase 4 corpus — comes after the first calibration panel.

### 3.5 Evaluation

The embedding is good if:

1. **Retrieval recall@k** for held-out same-wine pairs is high.
   Withhold 10% of source-records; embed both halves; check that
   the matched half appears in the top-k nearest neighbors.
   Target: recall@5 > 0.90 for wines with ≥ 3 reviews.
2. **Variety clustering** is meaningful. UMAP-project the
   embeddings, color by variety, check that varieties form
   visually coherent clusters.
3. **Vintage adjacency** is captured. For the same producer, the
   2017 and 2018 of a wine should be closer to each other than
   to a random other producer's 2018.
4. **Sommelier sanity check.** Sample 50 wine pairs; ask a
   sommelier "are these similar?"; check correlation with
   embedding distance. Target Spearman ρ > 0.6.

### 3.6 Inference + storage

- Store every wine's embedding in `wine_features.embedding`
  (Postgres `vector(256)` via pgvector).
- Maintain an IVF or HNSW index for fast similarity search.
- Recompute embeddings when (a) the model is retrained, or (b) a
  wine's features materially change (new reviews added, GC-MS
  data acquired). Use a `embedding_version` column to track which
  model produced which vector.

---

## Phase 4 — Personalized recommendations

### 4.1 The setup

A user provides ≥ 5 wines from our database with **their own
descriptions** of those wines. Examples:

```
("WT-2024-FR-BUR-014", "earthy, not too fruity, perfect Sunday wine")
("WT-2024-FR-BOR-082", "too tannic for me but loved the dark fruit")
("WT-2024-IT-NEB-047", "this is the platonic ideal of grippy")
("WT-2024-US-PIN-023", "fruit-bomb, give me less of this")
("WT-2024-DE-RIE-051", "razor acid, would buy again")
```

The goal: when this user later types a description like
*"something earthy and grippy, not too sweet"*, surface wines
the user is statistically likely to enjoy — and crucially, do it
**through the user's own meaning of "earthy" and "grippy"**, not
the generic meaning.

### 4.2 Why naive nearest-neighbor won't work

If we just embed the user's query and find nearest wines, we use
the generic language model's notion of "earthy". But every user
uses "earthy" slightly differently. "Grippy" especially varies:
some users mean firm tannin polymerization; some mean astringency
plus alcohol; some mean a bitter finish.

We need to learn the user's personal mapping from their words to
the wine-embedding space.

### 4.3 The model

Two paths, in order of complexity:

#### Path 1 — Ridge regression (recommended for PoC)

For each (user-description, wine) pair the user provides:

```
L_i = sentence_encoder(description_i)           # 768-dim
W_i = wine_embedding(wine_i)                     # 256-dim
```

Fit a personal linear transform `A_user, b_user` such that:

```
W_i ≈ A_user · L_i + b_user
```

With only ~5 samples, this overfits hard. Regularize toward a
**global prior** `(A_0, b_0)` learned from many users' labeling
data (or from the WineEnthusiast review corpus, which gives us a
language→embedding pair per wine for free):

```
loss = Σ_i || W_i − (A_user L_i + b_user) ||²
     + λ_A || A_user − A_0 ||²
     + λ_b || b_user − b_0 ||²
```

Solve analytically — ridge regression has a closed form.
Computational cost: milliseconds. Storage cost per user: ~200KB
(the A matrix plus bias).

**Query time:**

```python
def recommend(user, query_text, k=10):
    L = sentence_encoder(query_text)
    target = user.A @ L + user.b              # projected into wine space
    return vector_search(wine_features.embedding, target, k=k)
```

#### Path 2 — Bayesian / Gaussian-process refinement

Replace ridge with a Gaussian process whose mean function is `A_0
L + b_0` and whose covariance is learned from the global corpus.

Advantage: gives calibrated uncertainty per recommendation,
which means we can show the user wines we're confident they'll
like vs. exploratory picks.

Cost: more engineering, more compute per query.

Build this in Path-2 after Path 1 ships.

#### Path 3 — Meta-learning (research direction)

Train a meta-learner (MAML-style) over many simulated users such
that the few-shot adaptation step (5 samples → personalized
projection) is built into the model. Most expressive, most
complex. Don't pursue until Path 1 is in production and we have
real user-calibration data to evaluate against.

### 4.4 Recommendation policy

Given the projected target vector `target`, we can do more than
just "nearest neighbor":

- **Pure similarity** — return top-k by cosine distance.
- **Similarity + diversity** — return top-k MMR (Maximal Marginal
  Relevance) to avoid showing five Pinots from the same producer.
- **Similarity + uncertainty exploration** — mix top-3 highest-
  similarity picks with top-2 highest-uncertainty picks (the
  user's calibration improves fastest from labeling those).
- **Constraint filtering** — user says "under $50" or "drink
  tonight"; filter before similarity-rank.

### 4.5 Cold-start path

A first-time user with 0 wines labeled gets the **global prior**
projection (`A_0`, `b_0` from §4.3). This is just generic
language → wine similarity, no personalization.

After 5 labels: switch to ridge-personalized.

After ~30 labels: re-train with a personalized loss that weights
the user's own labels more heavily than the global prior — i.e.
λ shrinks as user data accumulates.

### 4.6 Feedback loop

Every time a user accepts or rejects a recommendation, log that
as an implicit label:

- **Accept** ("ordered it", "bought it", clicked through to
  purchase): treat as a positive label near the projected target.
- **Reject** ("dismissed", "not interested"): treat as a negative
  label.

Periodically re-fit each user's `A_user`, `b_user` with the
expanded label set.

### 4.7 Evaluation

The recommender works if:

1. **Personalized retrieval beats global retrieval on held-out
   labels.** For each user with > 10 labels, hold out 30%, fit on
   70%, check whether held-out preferred wines rank higher
   under personalized vs. global. Target lift: > 20% in NDCG@10.
2. **The user's stated "grippy" maps to a different chemical
   region than another user's.** Take two users with the
   word "grippy" in their labels; check that their projected
   targets are distinguishably different in wine-embedding space.
3. **Recommendations beat baseline.** A/B test:
   personalized-WineTone vs. "wines rated > 90 by sommeliers".
   Track conversion at whatever the user-facing surface is.

---

## Cross-cutting concerns

### Refresh cadence

- Tier A datasets: re-pull quarterly.
- Tier B scrapers: incremental delta-pull weekly.
- Tier C APIs: per their rate-limit / cost budget.
- Embedding model: re-train monthly initially, quarterly once
  stable, immediately on architecture changes.
- User projections: re-fit on every new label.

### Reproducibility

Every wine's stored embedding carries:
- `embedding_model_version` (e.g. `bge-wine-v2.3-fusion-v1.0`)
- `embedding_features_hash` — hash of the inputs that produced it
- `embedding_created_at`

Same for user projections. This way we can answer "why did we
recommend X to Y on day Z" months later.

### Costs (estimates, monthly)

- Cloud compute for scrapers: ~$50–200
- Postgres + pgvector hosting (medium instance + 200GB): ~$200
- Object store for raw payloads (~100GB): ~$5
- Embedding training GPU time (monthly retrain on a 24GB GPU,
  ~6 hours): ~$30
- Inference (sentence transformer + lookup): ~$50 for PoC
  traffic
- **Total operational: ~$350–500 / month**

Plus the one-time GC-MS costs (Phase 1D): $5k–9k per cohort.

### Privacy and user data

- User labels stay private to the user. Aggregated label
  patterns (e.g., "users with 'grippy' tend to cluster around
  Nebbiolo") are computed only over opted-in users.
- Users can export their PalatePrint at any time (just `A_user`,
  `b_user`, and their labels).
- Users can delete everything via one button — drop their row in
  `user_palates` and their entries in `user_labels`.
- Don't train the global embedding model on user-private labels
  without explicit opt-in.

### Open-data publication

The annual WineTone Palette release (`PLAN.md` §"Phase 4")
should publish:
- All Tier A + Tier B wines' embeddings
- All commissioned-Tier-D chemistry
- A *redacted* user-labeling corpus (only opted-in, only with
  labels generalized to taxonomy terms)

This becomes the citable WineTone reference dataset — the moat
described in `PLAN.md` §"Moat & IP".

---

## Decisions you need to make

Before kicking off implementation, the following are real forks
where the answer changes the architecture. Putting them here so
they don't ambush us later.

### D1 — Scrape Vivino or skip it?

Vivino has 50M+ reviews — by far the largest UGC corpus. Their
ToS prohibits scraping. Three options:

- **(A)** Pay for their affiliate API. Limited fields, paid.
- **(B)** Partner directly. Requires a sales conversation.
- **(C)** Skip Vivino entirely. Use CellarTracker + WineEnthusiast
  as the UGC core.

PoC recommendation: **(C)** skip. The 130k WineEnthusiast reviews
plus public-API CellarTracker is enough to bootstrap. Revisit
Vivino at growth stage.

### D2 — How aggressive on TTB COLA scraping?

TTB has ~500k labels. Each label page is one HTTP request. At 1
QPS that's six days of scraping. Acceptable. But:

- The labels contain the legally-registered wine name + producer
  + ABV + vintage + variety. **This is the single most valuable
  data source for entity resolution.** Without it, matching is
  much harder.

Recommendation: **scrape it all, respecting `robots.txt` and rate
limits, persist to object store for re-parsing.**

### D3 — GC-MS partner

UC Davis V&E, Geisenheim University, Adelaide, ETH Zurich are
all options. PoC needs 20–30 wines through full GC-MS:

- UC Davis: closer for US-based wines, ~$200/wine fully loaded.
- Geisenheim: best-known wine chemistry program, German wines
  cheap to source there.

Recommendation: **strike a partnership with one academic lab for
PoC.** Geisenheim if the wine cohort is European, UC Davis
otherwise.

### D4 — Embedding dimensionality

`PLAN.md` proposed 32-dim WinePrint. This document is proposing
256-dim for the operational embedding (with the 32-dim being
useful for the human-facing "palette chip" projection).

The choice: do we keep both, or just one?

Recommendation: **maintain two columns.**
- `embedding_256` — for ML / similarity search
- `wineprint_32` — for the Palette product, derived as a UMAP
  projection of `embedding_256`

That preserves the Pantone-chip vibe of the WineTone Palette
product without forcing the ML layer into 32 dimensions.

### D5 — User base & ToS for label data

If we deploy the personalization layer at the consumer level,
we're suddenly collecting user data at scale. This requires:

- A privacy policy.
- A ToS that establishes the user owns their labels and we have
  a license to use them for personalization.
- An opt-in for aggregated training.

This is non-trivial paperwork; allocate ~1 week of legal review
when we get to public launch.

### D6 — Open-source vs proprietary

The plan as currently written is implementation-as-Apache-2.0
(the repo's license). But the moat in `PLAN.md` §"Moat & IP" is
the **dataset**, not the code. So:

- Code: Apache-2.0 (current).
- Models (the fine-tuned embedding model): we can publish or
  reserve, that's a strategic call.
- Data (the annual WineTone Palette release): can be CC-BY-NC
  (free for research, paid for commercial use) — that's a
  common dataset license pattern.

Recommendation: **code open, models open, data tiered (free
research / paid commercial).** Matches the strategic posture of
projects like CommonCrawl or LAION.

---

## What's *not* in this plan (yet)

- **Sensory taxonomies.** The structured tasting-vocabulary
  taxonomy from `PLAN.md` §2B is referenced as input but not
  developed here. That's a separate document.
- **Wine fraud detection.** Mentioned as an adjacency in
  `PLAN.md` but not built into this plan; it's a downstream use
  case of the embedding model + GC-MS archive.
- **Multi-language support.** Reviews in French, Italian,
  Spanish, German exist. Sentence transformers are
  English-centric. Multilingual fine-tuning is a v2 concern.
- **Real-time data freshness.** Everything assumed batch /
  daily. If we ever need real-time (e.g., live auction prices),
  that's a separate streaming pipeline.

---

*v0.1 of the data + ML plan. Companion to [`PLAN.md`](../PLAN.md)
(the product concept) and [`README.md`](../README.md) (the
elevator pitch). Comments via GitHub Issues at
<https://github.com/archisgore/WineTone/issues>.*
