# Progress log

A running log of what's been built, in reverse chronological order.
For the plan being executed, see
[`DATA-AND-ML-PIPELINE-PLAN.md`](DATA-AND-ML-PIPELINE-PLAN.md).

---

## 2026-05-19 · Phase 1 Sprint 2 — Tier B kickoff

**Status:** partial. Two new sources working; TTB COLA deferred to
Sprint 3 with a written engineering plan.

### What landed

- `src/winetone/sources/wikidata.py` — Wikidata SPARQL via a polite
  User-Agent. Cross-reference anchor for Phase 2 entity resolution
  via stable Q-IDs for wines / wineries / regions / varieties.
- `src/winetone/sources/wine_enthusiast_150k.py` — the WineEnthusiast
  v1 corpus (150,930 rows). Complement to the 130k v2 — same source
  site but different scrape, different rows.
- `docs/SCRAPER-PLAN-TTB.md` — full engineering plan for the TTB
  COLA scraper. Multi-day backfill, resumable state, rate-limit
  policy, field-mapping table. Sprint 3 deliverable.
- CLI: `--tier b` added; new `make pull-tier-b` target.

### Captured run

```
                                 staged sources
┏━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━┓
┃ source               ┃    rows ┃    size ┃
┡━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━┩
│ uci_wine             │     178 │ 15.3 KB │
│ uci_wine_quality     │   6,497 │ 89.7 KB │
│ wikidata             │   5,333 │ 86.7 KB │
│ wine_enthusiast_130k │ 129,971 │ 23.1 MB │
│ wine_enthusiast_150k │ 150,930 │ 21.9 MB │
└──────────────────────┴─────────┴─────────┘
total: 292,909 rows · 45.2 MB
```

### Pivots during the sprint

- **OpenFoodFacts dropped, WineEnthusiast 150k added.** Sprint plan
  called for OpenFoodFacts as the second Tier B source, but
  `world.openfoodfacts.org` returned an HTML 503 page during the
  probe ("Page temporarily unavailable"). Pivoted to the
  WineEnthusiast 150k v1 corpus, which is reliably mirrored on
  GitHub and gives us complementary review rows. OpenFoodFacts goes
  back on the backlog.
- **Wikidata sub-queries collapsed to one.** Initial design had
  three SPARQL queries (wines + standalone wineries + standalone
  varieties). The standalone wineries query
  (`?winery wdt:P31 wd:Q420684`) returned zero rows — Wikidata
  rarely uses strict P31 typing for winery entities. Same for
  varieties. Pivoted to one wines query with OPTIONAL bindings
  for manufacturer / variety / country / region — same surface,
  simpler, no timeouts.

### Engineering lessons logged

- **Wikidata's label service returns the Q-ID when no English
  label exists.** ~12% of our 5,333 wine entries have labels like
  `Q4497289` (the Q-ID itself) because no human-readable label has
  been entered. Useful for entity resolution as a stable anchor,
  but downstream consumers should treat `label.startswith("Q")` +
  `label[1:].isdigit()` as "no friendly name".
- **`Series.map(fn)` on object columns hits NaN floats.** Pandas
  inserts NaN (float) for SPARQL OPTIONAL columns where the binding
  is absent. Our `_qid` helper needed an `isinstance(uri, str)`
  guard.

### Next sprint (Sprint 3)

Per `docs/SCRAPER-PLAN-TTB.md`:

- [ ] TTB COLA scraper end-to-end on a 1-month date window
      (acceptance gate: 10k+ records, resumable, ≤ 1 QPS).
- [ ] EU PDO/PGI registry (eAmbrosia) — API or CSV bulk.
- [ ] France INAO bulk download from data.gouv.fr.

After Sprint 3, **Phase 2 entity resolution becomes the critical
path** — we have enough corpus volume (~700k records anticipated)
that the deduplication / canonicalization work pays for itself.

---

## 2026-05-19 · Phase 1 Sprint 1 — Tier A acquisition

**Status:** complete.

Three Tier A sources pulled, parsed, and staged as Parquet on
disk. Reproducible with one command:

```
make dev
make pull-tier-a
make status
```

Captured run on fresh checkout:

```
                                 staged sources
┏━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━┓
┃ source               ┃    rows ┃    size ┃
┡━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━┩
│ uci_wine             │     178 │ 15.3 KB │
│ uci_wine_quality     │   6,497 │ 89.7 KB │
│ wine_enthusiast_130k │ 129,971 │ 23.1 MB │
└──────────────────────┴─────────┴─────────┘
total: 136,646 rows · 23.2 MB
```

### What landed

| Piece | Where |
|---|---|
| Project layout | `pyproject.toml`, `src/winetone/`, `tests/` |
| Filesystem layout | `src/winetone/paths.py` (`data/raw/`, `data/staging/`) |
| Source protocol | `src/winetone/sources/base.py` — `Source` ABC + retry + hash + Parquet output |
| UCI Wine Quality | `src/winetone/sources/uci_wine_quality.py` — red + white, 6,497 rows, 13 cols |
| UCI Wine 178 | `src/winetone/sources/uci_wine.py` — 3-cultivar, 14 cols (incl. cultivar_id) |
| WineEnthusiast 130k | `src/winetone/sources/wine_enthusiast.py` — Kaggle mirror with 3 fallback URLs |
| Registry | `src/winetone/sources/__init__.py` — `SOURCES: dict[str, type[Source]]` |
| CLI | `src/winetone/cli.py` — `winetone list / pull / inspect / status` (entry point via `pyproject.toml`) |
| Makefile | `make venv / install / dev / pull-tier-a / status / inspect / test / lint / clean` |
| Tests | `tests/test_registry.py` — registry sanity, network-free |

### Schema highlights

`uci_wine_quality` (6,497 rows; combined red + white):

```
fixed_acidity, volatile_acidity, citric_acid, residual_sugar,
chlorides, free_sulfur_dioxide, total_sulfur_dioxide, density,
ph, sulphates, alcohol, quality, wine_color
```

`uci_wine` (178 rows; 3 Italian cultivars):

```
cultivar_id, alcohol, malic_acid, ash, alcalinity_of_ash,
magnesium, total_phenols, flavanoids, nonflavanoid_phenols,
proanthocyanins, color_intensity, hue, od280_od315, proline
```

`wine_enthusiast_130k` (129,971 rows; English-language wine reviews):

```
country, description, designation, points, price, province,
region_1, region_2, taster_name, taster_twitter_handle, title,
variety, winery
```

### Lessons / decisions made

- **Kaggle authentication is friction.** The WineEnthusiast 130k
  dataset is officially hosted on Kaggle and requires API
  credentials to pull from there. Three independent github
  mirrors all return the canonical CSV under
  `raw.githubusercontent.com`. We try mirrors in order and only
  surface an error if every mirror fails, in which case we point
  the user at the official Kaggle source. This is brittle in
  principle (mirrors can disappear) but works today and is the
  fastest way to bootstrap; later we'll add a proper Kaggle-API
  path for Phase 1 Sprint 2 once we know we want this corpus
  long-term.

- **pandas 3.0 changed `read_parquet(columns=[])`.** Returns 0
  rows now where 2.x returned a metadata-only frame. Switched
  `winetone status` to read row counts from
  `pyarrow.parquet.ParquetFile.metadata.num_rows` directly.

- **Python 3.14's `datetime.UTC` alias.** Ruff's `UP017` autofixed
  `datetime.timezone.UTC` to `datetime.UTC`. Minimum Python
  bumped to ≥ 3.11 in `pyproject.toml`; 3.14 is the dev target.

### Next sprint (Phase 1 Sprint 2)

The plan's Tier B sources, starting with the highest-value entry-
resolution anchor:

- [ ] **TTB COLA** scraper (`src/winetone/sources/ttb_cola.py`)
      — US-registered wine labels, ~500K records. The single
      most useful data source for entity resolution because every
      label has the legally-registered producer + brand + vintage
      + variety. Rate-limited polite scraping; multi-day fetch
      checkpointed to disk.
- [ ] **EU PDO/PGI registry** (eAmbrosia API).
- [ ] **France INAO** bulk download.
- [ ] **Wikidata SPARQL** for producer / region cross-references.

After Tier B, Phase 2 (entity resolution) becomes the critical path.

---

*Older sprints will appear above this line as the log accumulates.*
