# Your "Oaky" Isn't My "Oaky" — And That's a Recommender Problem

*A blueprint for recommendation systems that listen to what the
user actually means, not what the dictionary says.*

---

Pantone works because the universe of color is a small, agreed-on,
calibrated thing. Pull out a Pantone chip and the conversation
between you, the printer, the paint factory, and the website
designer is *over* — that's the color. Done.

Now try this in wine.

You say *"give me something oaky."* What did you just say?
Depends on whose nose heard it. To a sommelier, "oaky" is
shorthand for vanilla, coconut, baking spice, the toast of a
freshly charred new French barrel. To a bourbon drinker,
"oaky" is caramel and leather. To a carpenter, "oaky" is
fresh-cut wood and sawdust. To me — having grown up in
India — "oaky" arrives smelling like sandalwood incense and
the smoke off a tandoor, because that's the wood-and-fire
reference set my palate was assembled from. To someone who's
never tasted a wine aged in new oak, "oaky" doesn't even map
to a specific word; they taste the molecule and pull out
something completely different.

Same molecule. Five vocabularies. Pick a winner.

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

Query: *"earthy and oaky with jasmine notes."*

**Without calibration**, the system returns:

| # | wine |
|---|---|
| 1 | Rooster Hill Medium Sweet Riesling (US) |
| 2 | Ravenswood Old Hill Zinfandel (US) |
| 3 | Jarvis Cabernet Franc (US) |
| 4 | Hermann J. Wiemer Riesling (US) |
| 5 | Eichinger Riesling (Austria) |

A reasonable mix. The dictionary says "earthy and oaky" so the
system finds wines that the *corpus* describes with adjacent
words. Sweet Riesling shows up because "earthy" lives in
American Riesling reviews. Zin and Cab Franc are there because
"oaky" co-occurs with "vanilla," "spice," and "toast" in
New-World reds. Fine — it's not *wrong*. It just isn't *me*.

Now I calibrate. I label six wines I've actually drunk. The
calibrating label that matters most is on a Barolo, where I write:

> *"tar and roses, oaky like a Pottery Barn, jasmine on the finish."*

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
"oaky" lives in Nebbiolo territory of the embedding space.
"Jasmine" got pulled toward the floral end of Barolo's aromatic
signature. The dictionary's "oaky" didn't move. *My* "oaky"
moved to where the wines I'd actually order live.

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

## Don't ask the LLM to be your database

A subtle architectural point worth surfacing on its own: **most of
WineTone never touches an LLM at all.**

The popular RAG (retrieval-augmented generation) pattern has
people piping their corpus through a vector store and then
handing the retrieved chunks back to an LLM to *assemble* an
answer. Useful for some problems. Wrong for this one.

WineTone uses an embedding model (bge-small-en-v1.5 — 33M params,
not really an LLM in the conversational sense) for exactly one
job: **turning text into a vector**. Once vectors exist, every
other operation is linear algebra inside **CedarDB** (a fast,
Postgres-wire-compatible analytical database with pgvector built
in):

- Canonical wine resolution: deterministic SQL string-matching.
- Similarity search across 164,069 wines: a pgvector dot product.
- Aggregation, filtering ("only French wines"), joins between
  reviews and metadata: SQL.
- Personal projection fitting: closed-form ridge regression, or a
  384×384 linear layer trained in milliseconds. Pure linear
  algebra.
- Cluster summarization, top-k retrieval, hybrid scoring: numpy.

No LLM in any of that. The encoder gets called exactly:

- **Once per wine** at corpus-build time → cached forever as a
  384-dim row in CedarDB.
- **Once per user query** at recommend time → ~30ms.
- **Once per user label** at calibration time → ~30ms.

Three benefits worth being explicit about:

1. **Token cost collapses.** A naive design might pass the user's
   query plus a hundred candidate descriptions to GPT-4 and ask
   *"which fits best?"*. That's 100× the tokens per query, and
   it grows with the catalog. WineTone embeds the query once and
   lets CedarDB rank against the precomputed corpus. Steady-state
   per-query cost is microcents.

2. **Speed.** A CedarDB scan of 164K pgvector rows for cosine
   similarity is under 50ms. An LLM ranking 164K candidates is
   physically impossible at any latency. (You'd prune to ~20
   with a vector DB first — and at that point why not just take
   the database's answer and skip the round trip?)

3. **No hallucination, by construction.** The LLM never produces
   wine names. It never claims a wine has certain notes. It never
   invents a vintage. Every wine in a result table came from a
   CedarDB row ID with full provenance — every producer, vintage,
   region traceable back to the public source it was scraped
   from. The dataset is the source of truth; the recommendation
   is deterministically derived from it. Nothing is being
   *generated*; everything is being *selected*.

The pattern is the takeaway: **let the encoder do the one thing
encoders are good at — semantic compression of language — and
let the database do everything else.** "Use the LLM for the LLM
part" turns out to be most of the design wisdom you need.

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

Your "oaky." My "oaky." Someone else's "vanilla." Same wood,
different word.

The dictionary version of those words is a flattening. WineTone
is a small experiment in what's possible when you stop flattening
and start *calibrating*.

It's also a pretty good way to find a Barolo.

---

*Code, plan, and reproducible demo at
[github.com/archisgore/WineTone](https://github.com/archisgore/WineTone).
Apache-2.0. Concept by Archis Gore — wine-adjacent angel investors
and academic chemistry labs welcome.*
