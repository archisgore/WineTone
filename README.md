# WineTone

> *Pantone for wine.*

A coordinate system that turns wine from a subjective experience into
an objective reference: every bottle has a high-dimensional chemical
fingerprint (the **WinePrint**), every palate has a calibrated
position in that space (the **PalatePrint**), and matching them is a
solved problem.

## The three pillars

| | What it is | Analogy |
|---|---|---|
| **WinePrint** | Chemical fingerprint of a wine | Pantone chip |
| **PalatePrint** | Calibrated personal taste vector | Monitor color profile |
| **WineTone Palette** | Annual distributable embedding set | Pantone color book |

## Status

**Concept, not yet built.** Two planning documents:

- [`PLAN.md`](PLAN.md) — the v0.1 product concept (the *why* and
  the elevator-level *what*).
- [`docs/DATA-AND-ML-PIPELINE-PLAN.md`](docs/DATA-AND-ML-PIPELINE-PLAN.md)
  — the v0.1 implementation plan: data acquisition across all
  public sources, the entity-resolution / normalization
  pipeline, the embedding model, and the personalization layer
  that learns a user's labeling style from 5+ samples.

The implementation plan is end-to-end specific: source
inventory by tier (free → ToS-gated → commissioned),
entity-resolution algorithm, schema, embedding architecture
(multi-modal contrastive), and the few-shot personalization
math (ridge regression with global-prior regularization).

Estimated PoC budget: **$7k–15k**, dominated by GC-MS analysis at an
academic chemistry lab.

## What's in this repo

```
WineTone/
├── README.md            this file
├── PLAN.md              v0.1 product concept
├── docs/
│   └── DATA-AND-ML-PIPELINE-PLAN.md   v0.1 implementation plan
├── CONTRIBUTING.md      how to get involved
├── LICENSE              Apache-2.0
├── .gitignore
└── .editorconfig
```

No code yet. The plan identifies the tech stack (Python +
scikit-learn + UMAP for embeddings, Postgres + pgvector for storage,
FastAPI for API, React + Tailwind for UI) and the academic-lab
partners needed for GC-MS access (UC Davis Viticulture & Enology,
Geisenheim University, or equivalent).

## What WineTone is *not*

- **Not a wine review app.** Vivino already exists.
- **Not a recommendation engine over star ratings.** Star ratings
  are aggregated subjective experience; we want the chemistry
  underneath.
- **Not a marketing-copy generator.** The point is the inverse —
  *ground* sensory language in measurable chemistry so "dry" and
  "grippy" stop meaning different things to different people.

## Adjacencies worth thinking about

- **Authentication / fraud detection** — compare claimed vintage
  against the WinePrint archive.
- **Winemaker tooling** — "make this year's batch match last year's
  WinePrint within cosine distance X."
- **Medical / sensitivity** — identify compounds correlating with
  histamine response or migraine triggers.
- **Spirits extension** — the same architecture works for whisky,
  sake, olive oil, perfume.

## Who to talk to

If you can help with any of these, please open an issue or email:

- **Academic GC-MS access** — UC Davis V&E, Geisenheim, Adelaide,
  or an equivalent enology chemistry lab.
- **Wine data partnerships** — Wine Folly, CellarTracker, Vivino,
  WineEnthusiast.
- **Tasting panel recruitment** — 10–20 calibrated tasters for the
  initial calibration corpus.
- **ML engineering** — dimensionality reduction, fine-tuning a small
  LLM as the language-to-chemistry bridge, vector similarity at
  query time.
- **Angel funding** — wine-adjacent or luxury-consumer.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the rest.

## Author

**Archis Gore** — concept author. Background includes the
[Encrypted Execution](https://www.encrypted-execution.com) thesis
work and the
[Polyverse](https://medium.com/polyverse) polymorphic-Linux line.
Email: `me@archisgore.com`.

## License

Apache-2.0. See [`LICENSE`](LICENSE).
