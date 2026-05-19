# WineTone — PoC Architecture Plan
**Concept by Archis | Version 0.1**

> A Pantone-like objective reference system for wine: high-dimensional chemical fingerprints, personal palate calibration, and distributable annual "embedding palettes."

---

## The Core Idea in One Sentence

Convert wine from a subjective experience into an objective coordinate system — where every bottle has a unique chemical fingerprint, every palate has a calibrated position in that space, and matching them is a solved problem.

---

## Three Pillars

| Pillar | What it is | Analogy |
|---|---|---|
| **WinePrint** | Chemical fingerprint of a wine | Pantone chip |
| **PalatePrint** | Calibrated personal taste vector | Monitor color profile |
| **WineTone Palette** | Annual distributable embedding set | Pantone color book |

---

## Phase 1 — Data & Fingerprinting

### 1A. Chemical Analysis Pipeline

For each wine sample, collect:

- **Gas Chromatography–Mass Spectrometry (GC-MS):** ~200–400 volatile aromatic compounds (esters, terpenes, aldehydes, acids)
- **Standard Physicochemical Panel:** pH, titratable acidity, residual sugar, alcohol %, SO₂ (free + total), turbidity
- **Colorimetry:** CIE Lab* values (objective color, not just "ruby" or "garnet")
- **Polyphenol Panel:** Total phenolics, anthocyanins, tannin structure (polymerization index)
- **Optional advanced:** NMR spectroscopy for geographic provenance fingerprinting

**Output:** A raw vector of ~500–600 dimensions per wine per vintage.

### 1B. Dimensionality Reduction → The WinePrint

Reduce the raw chemical vector to a manageable embedding:

```python
# Conceptual pipeline
raw_vector = gc_ms_output + physicochemical + colorimetry  # ~600 dims
winepprint = UMAP(n_components=32).fit_transform(raw_vector)
# or use a trained autoencoder for more expressive compression
```

Store both the full raw vector (archival) and the 32-dim WinePrint (operational).

**The WinePrint IS the canonical identity of that wine-vintage.** The last 10 bottles of a 1945 Pétrus, digitized forever.

### 1C. Seed Dataset (PoC Scale)

Start with ~200–500 wines across:
- 5–6 major varietals (Cab Sauv, Pinot Noir, Chardonnay, Riesling, Nebbiolo, Syrah)
- 3–4 regions per varietal
- 3–5 vintages per wine where possible

**Existing data sources to leverage:**
- UCI Wine datasets (free, immediate)
- Wine Folly DB API (structured chemical + sensory)
- Commission 20–30 wines for actual GC-MS analysis (~$150–300/wine at academic labs)

---

## Phase 2 — Language Calibration

### 2A. Building the Vocabulary Bridge

The problem: "dry" means different things to different people. The solution: anchor language to chemistry.

Collect a calibration corpus:
- Take 50–100 chemically fingerprinted wines
- Have a panel of 10–20 tasters describe each wine using free text + structured attributes
- This creates a mapping: `language tokens → chemical dimensions`

Use the WineEnthusiast 130k review dataset (already exists, free on Kaggle) as a pre-training corpus. Fine-tune on your calibrated panel data.

```
wine_description: "dry, grippy tannins, dark fruit, long finish"
     ↓  trained embedding model
language_vector: [0.82, 0.14, 0.67, ...]
     ↓  calibrated projection
chemical_space_position: [WinePrint coordinates]
```

### 2B. Computational Wine Wheel

Define a structured vocabulary taxonomy (hierarchical):
```
TASTE
├── Sweetness (bone dry / off-dry / medium / sweet / luscious)
├── Acidity (flat / low / medium / high / razor)
├── Tannin (none / silky / medium / firm / grippy / harsh)
└── Body (light / medium / full / massive)

AROMA — PRIMARY (fruit, floral, herbal)
├── Red Fruit (cherry / raspberry / strawberry / cranberry)
├── Dark Fruit (blackberry / plum / cassis / fig)
├── Citrus (lemon / grapefruit / lime / orange)
└── ...

AROMA — SECONDARY (fermentation)
AROMA — TERTIARY (oak, aging)

FINISH
├── Length (short / medium / long / infinite)
└── Character (clean / tannic / acidic / bitter / spiced)
```

Map every node to its chemical correlates. Tannin grip → tannin polymerization index. Citrus → limonene + citral GC-MS peaks. This is the Rosetta Stone.

---

## Phase 3 — PalatePrint (Personal Calibration)

### 3A. Calibration Protocol

A user needs only 5–10 wines and their descriptions to get calibrated.

```
INPUT:
  Wine A (WinePrint known) → User says: "too tannic, love the dark fruit"
  Wine B (WinePrint known) → User says: "perfect acidity, a bit thin"
  Wine C (WinePrint known) → User says: "too sweet for me"
  ...

OUTPUT:
  PalatePrint = preference vector in WinePrint space
  + sensitivity weights per chemical dimension
  + language calibration (what THEIR "dry" means chemically)
```

This is essentially few-shot personalization — a lightweight fine-tune or a Bayesian update over the chemical space.

### 3B. Palate Profiles

```json
{
  "palate_id": "archis_001",
  "sweetness_tolerance": 0.2,
  "tannin_preference": [0.5, 0.75],
  "acidity_preference": 0.7,
  "preferred_aroma_clusters": ["dark_fruit", "earthy", "spice"],
  "language_calibration": {
    "dry": 0.15,
    "grippy": 0.72
  }
}
```

### 3C. Sommelier / Judge Calibration

The same protocol applied to professionals gives you an objective map of critical bias. Is Parker systematically higher on alcohol-forward wines? Does a particular competition judge over-weight oak? Now you can measure it.

---

## Phase 4 — The WineTone Palette (Product)

### Annual Palette Release

Each year, a curated set of WinePrint embeddings is published:
- 500–1000 wines analyzed that vintage
- Distributed as a structured data file (JSON / Parquet)
- Licensed to: restaurants, retailers, importers, competition judges, apps

```json
{
  "winetone_id": "WT-2024-IT-NEB-047",
  "wine": "Barolo DOCG",
  "producer": "Giacomo Conterno",
  "vintage": 2021,
  "winepprint_32": [0.82, 0.14, 0.67, 0.33],
  "colorimetry": { "L": 28.4, "a": 12.1, "b": -3.2 },
  "structured_profile": {
    "sweetness": 0.05,
    "acidity": 0.88,
    "tannin": 0.91,
    "body": 0.85,
    "primary_aromas": ["tar", "rose", "cherry", "leather"],
    "finish_length": 0.95
  },
  "drink_window": [2026, 2045]
}
```

A buyer can now say: "I want wines within cosine distance 0.15 of WT-2024-IT-NEB-047 but with lower tannin." That's a solved query.

---

## PoC Build Plan (90 Days)

### Month 1 — Data Foundation
- [ ] Download + clean UCI Wine datasets and WineEnthusiast 130k (free, immediate)
- [ ] Integrate Wine Folly DB API for structured chemical + sensory data
- [ ] Commission GC-MS analysis on 20–30 wines (budget: ~$5–8k at university lab)
- [ ] Build raw vector schema and storage (Postgres + pgvector)

### Month 2 — Embedding + Calibration
- [ ] Train dimensionality reduction (UMAP or autoencoder) on combined dataset
- [ ] Build vocabulary bridge: map WineEnthusiast descriptions → chemical dimensions
- [ ] Run small calibration panel (10 people × 10 wines each)
- [ ] Build PalatePrint generation from 5-wine input

### Month 3 — Demo Product
- [ ] Simple web UI: input 5 wine descriptions → get your PalatePrint
- [ ] Query interface: "find wines like this but more acidic"
- [ ] Export a mini WineTone Palette (50 wines) as JSON
- [ ] One-pager + demo video for investor/partner outreach

---

## Tech Stack (Recommended)

| Layer | Choice | Why |
|---|---|---|
| Data storage | Postgres + pgvector | Native vector similarity search |
| Embeddings | Python, scikit-learn, UMAP | Fast iteration |
| Language model | Fine-tuned small LLM (Mistral 7B or similar) | Wine vocabulary bridge |
| API | FastAPI | Clean, fast |
| Demo UI | React + Tailwind | Rapid PoC |
| Lab partner | University chemistry dept or Geisenheim Institute (Germany) | GC-MS access |

---

## Budget Estimate (PoC)

| Item | Cost |
|---|---|
| GC-MS analysis, 30 wines | $5,000–9,000 |
| Cloud compute (embeddings, training) | $500–1,500 |
| Wine samples for analysis | $1,000–3,000 |
| Tasting panel (incentives) | $500–1,000 |
| **Total PoC** | **~$7k–15k** |

---

## Moat & IP

1. **The calibrated corpus** — once you have 500+ wines with both GC-MS fingerprints AND human language annotations, the dataset itself is the moat.
2. **PalatePrint methodology** — the few-shot palate calibration protocol.
3. **The WineTone ID system** — a universally referenceable wine identity standard (think ISBN for wines).
4. **Network effects** — every new palate calibration makes the language model more accurate.

---

## Adjacent Applications

- **Wine authentication / fraud detection** — compare claimed vintage against WinePrint archive
- **Winemaker tool** — "make this year's batch match last year's WinePrint"
- **Medical / sensitivity** — identify chemical compounds correlating with headaches / histamine response
- **Spirits extension** — the same architecture works for whisky, sake, olive oil

---

## Who to Talk To

- **Academic labs:** UC Davis Viticulture & Enology, Geisenheim University (Germany), University of Adelaide
- **Potential angels:** wine-adjacent tech investors, luxury consumer angels
- **Strategic partners:** Wine Folly (data), CellarTracker (distribution), Vivino (scale)
- **Builders needed:** ML engineer (embeddings), full-stack, analytical chemist

---

*WineTone v0.1 — Concept by Archis Gore, May 2026.*
