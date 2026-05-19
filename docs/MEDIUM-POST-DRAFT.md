# Your "Grippy" Isn't My "Grippy" — And That's a Recommender Problem

*A blueprint for recommendation systems that listen to what the
user actually means, not what the dictionary says.*

---

Pantone works because the universe of color is a small, agreed-on,
calibrated thing. Pull out a Pantone chip and the conversation
between you, the printer, the paint factory, and the website
designer is *over* — that's the color. Done.

Now try this in wine.

You say *"give me something grippy."* What did you just say? In a
sommelier's mouth, "grippy" usually means high tannin polymerization,
a textural drag on the gums. From me — having grown up in the
Middle East — "grippy" arrives wrapped in associations with jasmine
and saffron, because those are the reference flavors my palate
was assembled from. From someone who's never been to France,
"French oak" doesn't mean anything; they taste the same molecule
and pull out a different word entirely.

The molecule is the same. The vocabulary isn't.

**Every recommender system in existence treats your words as if
they had a fixed meaning.** They embed your query into a vector
space. They search. They give you back what *the corpus* says is
near. But the corpus was written by other people. Other palates.
Other reference sets. The recommender is, technically, listening
— but it's hearing you in someone else's accent.

WineTone is a prototype for the opposite construction. A
recommender that **calibrates the user, not just the corpus.**

---

## What it does in one paragraph

Pull every publicly available wine dataset we can legally touch.
Around 287,000 reviews, 164,069 distinct (producer, wine, vintage)
triples after canonicalization. Embed each wine into a 384-dimensional
vector that captures both *what it tastes like* (dense semantic
embedding from a sentence transformer) and *what words people use
about it* (sparse TF-IDF embedding over the review corpus). Now:
ask a user to label five-ish wines they've tried, in **their own
words**. Use those five points to fit a tiny personal projection —
a 384×384 matrix plus a bias — that maps *their* vocabulary into
the global wine space. Save that projection. Every subsequent
query from that user runs through it before the nearest-neighbor
search.

The math is closed-form ridge regression with an identity prior,
or gradient descent in PyTorch / MLX (auto-detected per machine).
Either way: milliseconds to fit, kilobytes to store, and the
calibration grows more accurate each time the user adds a label.

---

## The proof

Same query, same database, same model. Zero other changes.

Query: *"earthy and grippy with jasmine notes."*

**Without calibration**, the system returns:

| # | wine |
|---|---|
| 1 | Rooster Hill Medium Sweet Riesling (US) |
| 2 | Ravenswood Old Hill Zinfandel (US) |
| 3 | Jarvis Cabernet Franc (US) |
| 4 | Hermann J. Wiemer Riesling (US) |
| 5 | Eichinger Riesling (Austria) |

A reasonable mix. The dictionary says "earthy and grippy" so the
system finds wines that the *corpus* describes with adjacent
words. Sweet Riesling shows up because "earthy" appears in
American Riesling reviews. Zin and Cab Franc are there because
"grippy" lives in the same neighborhood as "tannin." Fine — it's
not *wrong*. It just isn't *me*.

Now I calibrate. I label six wines I've drunk recently. The
calibrating label that matters most is on a Barolo, where I say:

> *"tar and roses, grippy as a vice, jasmine on the finish."*

Same query again. Same database. **Personalized for archis:**

| # | wine | from |
|---|---|---|
| 1 | Beni di Batasiolo Boscareto | **Italy — Nebbiolo** |
| 2 | Mario Marengo Barolo | **Italy — Nebbiolo** |
| 3 | Viberti Buon Padre Barolo | **Italy — Nebbiolo** |
| 4 | Claverana Olo | **Italy — Nebbiolo** |
| 5 | EOS Estate Zinfandel | US |

Four of five are *Italian Nebbiolo from Piedmont*. The system
inferred — from one label on one Barolo — that this user's word
"grippy" lives in Nebbiolo territory of the embedding space.
"Jasmine" got pulled toward the floral end of Barolo's aromatic
signature. The dictionary's "grippy" stayed where it was; *my*
"grippy" moved to where the wines I'd actually order live.

This isn't novelty. The system didn't switch to a different
search engine. It learned **my accent**, and from then on it
hears my queries in it.

---

## Why this matters beyond wine

The wine example is concrete and tastable. But the construction is
not about wine.

- **A user searching for *"cat videos"*** may mean small house cats
  (the obvious default) or jungle cats (lions, tigers, snow
  leopards, the cool ones). Same word. Wildly different intent.
  The system that knows you've watched five David Attenborough
  documentaries doesn't have to ask.
- **A user searching for *"big cuddly bears"*** may be referencing
  Bernese Mountain Dogs or Alaskan Malamutes — not literal bears.
  They're describing a *vibe* the dictionary can't parse. The
  system that already learned their dog-shopping pattern
  understands the metaphor in stride.
- **A user asking for *"a birthday wine"*** is doing something
  semantically slippery on purpose. Is it a social occasion
  (give them a crowd-pleaser), or a personal indulgence (give
  them something rare and weird)? The same query has opposite
  answers depending on who's asking. A recommender that knows
  *who* you are doesn't have to guess.

In every one of these cases, the system that wins isn't the one
with more training data on the corpus side. It's the one that
**absorbs your idiom on the user side.**

That's the whole shape of WineTone. It's just a particularly
delicious instance.

---

## What's under the hood (lightly)

I won't bury the technical reader. The repo
([github.com/archisgore/WineTone](https://github.com/archisgore/WineTone))
has the full plan — but the load-bearing pieces are:

1. **A canonical wine table.** We pull from five public sources
   (WineEnthusiast 130k + 150k, UCI Wine Quality, UCI Wine,
   Wikidata SPARQL). Deterministic entity resolution on
   (producer, wine, vintage) collapses the 287K source records
   into 164K distinct wines. Stored in **CedarDB** — a fast,
   Postgres-wire-compatible analytical database with pgvector
   built in.

2. **Hybrid embeddings.** Each wine gets:
   - A *dense* 384-dim vector from `BAAI/bge-small-en-v1.5` via
     fastembed (ONNX runtime, no PyTorch overhead). Captures
     *what the wine tastes like.*
   - A *sparse* TF-IDF vector (50K vocab, 1+2-gram) over the full
     review corpus. Captures *what words people actually use about
     this wine.* Lexical precision the dense vector blurs.

3. **A per-user projection.** For each user, fit `W ≈ A·L + b`
   where `L` is the encoder's embedding of their description, `W`
   is the canonical wine embedding. Ridge regression with an
   identity prior keeps a cold user behaving like the generic
   baseline; each label perturbs `A` and `b` only in the
   directions the data demands. Auto-detected to **MLX** on Apple
   Silicon (Metal + unified memory) or **PyTorch CUDA** on NVIDIA
   hardware. Same math, ~milliseconds to fit, ~250KB to store.

4. **Versioned calibration history.** Every fit gets appended to
   `user_calibration_history` so we can watch the user's
   projection drift away from identity as they add labels — and
   show that the drift converges to a stable shape, not noise.

5. **A feedback loop.** Every user's labels stay in the database.
   On the *next* corpus rebuild, those user descriptions get
   folded into the review text the global encoder sees. The
   corpus grows with user vocabulary over time. Tomorrow's encoder
   has heard more accents than today's.

That's the whole architecture. Everything else is plumbing.

---

## Honest caveats

This is a research prototype, not a wine app. Some specific
limits worth flagging:

- The dense embeddings cover **20,000 of 164,069 wines** in the
  current build — full-corpus encoding on CPU would take ~2.7
  hours and I haven't been patient enough yet.
- The calibration's effective sample is **five to fifty** labels
  per user. With heavy regularization toward identity, that
  works; without it, you'd overfit instantly to a 384×384
  parameter matrix.
- It only speaks English. Wine reviews exist in French, Italian,
  Spanish, German, Portuguese, and a dozen others. A truly
  cross-cultural calibrator would need a multilingual encoder
  underneath.
- There's no actual chemistry in this prototype. The *real*
  WineTone — see `PLAN.md` in the repo — pairs the language
  embeddings with GC-MS chemical fingerprints from an academic
  lab. That's a budgeted $7K–15K of analytical chemistry away.

What works *is* working, and the math generalizes. The Nebbiolo
shift was not cherry-picked.

---

## The thesis, one more time

Recommendation systems have gotten very, very good at the word
side of the problem. LLMs taught the corpus to listen — every
attention head is a little machine for figuring out what *the
text* means. The next leap, I think, isn't bigger encoders. It's
recommenders that figure out what *the user* means. Not in
aggregate (we have demographic models for that, and they're
boring). Individually. One per person.

Your "grippy." My "grippy." Someone else's "earthy."

The dictionary version of those words is a flattening. WineTone
is a small experiment in what's possible when you stop flattening
and start *calibrating*.

It's also a pretty good way to find a Barolo.

---

*Code, plan, and reproducible demo at
[github.com/archisgore/WineTone](https://github.com/archisgore/WineTone).
Apache-2.0. Concept by Archis Gore — wine-adjacent angel investors
and academic chemistry labs welcome.*
