# Contributing to WineTone

WineTone is at the concept stage (v0.1). There is no code yet; the
[`PLAN.md`](PLAN.md) is the canonical artifact. The contributions
that move the needle right now are not pull requests — they're
conversations with the right people.

## What's most useful right now

In rough order of leverage:

1. **An academic chemistry lab partnership.** GC-MS on 20–30 wines
   is the costliest, slowest-to-arrange item on the PoC critical
   path. UC Davis Viticulture & Enology, Geisenheim University,
   University of Adelaide, or any enology chemistry program with
   GC-MS access. If you have a contact, please open an issue.

2. **A first tasting panel.** 10–20 calibrated tasters describing
   50–100 wines is what builds the language-to-chemistry Rosetta
   Stone (Phase 2 of `PLAN.md`). Wine professionals welcome but not
   required; calibration is about per-taster consistency, not
   absolute expertise.

3. **Data partnerships.** Wine Folly's structured chemical/sensory
   API, CellarTracker's user reviews, Vivino's scale — any of these
   accelerates the corpus-building phase significantly.

4. **ML engineering** (when there's code to write). The interesting
   problems: the 600 → 32-dim reduction, the language-token →
   chemical-dimension projection, and the few-shot PalatePrint
   calibration. None of these are open research questions; they're
   engineering with care.

5. **Angel funding.** PoC budget is ~$7k–15k, dominated by GC-MS
   costs. Wine-adjacent or luxury-consumer angels welcome.

## How to engage

- **Open an issue** at
  <https://github.com/archisgore/WineTone/issues> with the topic in
  the title. Use the `interest:` prefix to flag what you're
  interested in (e.g., `interest: lab partnership`, `interest:
  tasting panel`).
- **Email** `me@archisgore.com` for anything that shouldn't be
  public (partnership terms, lab pricing).

## Code contributions (later)

Once the repo has code:

- License is Apache-2.0. Contributions are accepted under those
  terms.
- Python style: Black + isort.
- TypeScript style: Prettier defaults.
- Test before you push.

## What we won't accept

- Marketing-style rewrites of the README or PLAN. Honest framing >
  selling.
- "Add AI" or "add blockchain" suggestions without a concrete
  problem they solve.
- Recommendations that boil down to a stars-based aggregation
  layer. Vivino exists.

## Maintainer

[@archisgore](https://github.com/archisgore).
