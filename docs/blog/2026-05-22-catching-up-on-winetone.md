# Catching up on WineTone

*Posted 2026-05-22*

It's been a while since the first post — the one where I introduced the
thesis and demoed the basic loop. Since then WineTone has gone from
"a script that proved the idea" to a real product running at
[tone.wine](https://tone.wine). Here's the inventory.

## The thesis hasn't changed

Same hook as before: recommendation models have figured out that
*words have context*, but they haven't figured out that *users have
personal context on how they use words*. "Grippy" from a Nebbiolo
drinker means tannic structure. "Grippy" from someone who grew up
tasting orange wines means something else. Same token, different
meaning — and the right recommendation depends on knowing which
is yours.

WineTone fits a per-user linear projection on top of a
384-dimensional wine-embedding space. Label five wines you know in
your own words; the projection learns the map from your vocabulary
into the catalog. The thesis is unchanged. What's changed is
everything around it.

## The corpus actually exists now

The first post ran the demo on a 20,000-wine stratified sample
because building dense embeddings on the full 164,069-wine corpus
took ~2.7 hours on a CPU. That sample worked for the demo but was a
real ceiling on recommendation quality — if your favorite Burgundy
producer wasn't one of the 20K, the model might as well not have
known they existed.

I bought patience: the full corpus is in pgvector now. Every wine
that survived canonicalization (producer + cuvée + vintage
deduplication) has a dense embedding, a TF-IDF sparse representation,
and full-text indexing. Queries that returned "no match" on day one
now find the right Châteauneuf-du-Pape.

## The encoder learned wine

The first post used the off-the-shelf `bge-small-en-v1.5`. It's a
good model, but it's trained on the open web — its embedding of
"petrol" lives near *gasoline*, not near *aged Mosel Riesling*. That
mismatch costs accuracy in the wine domain.

So I fine-tuned it. Two reviews of the same wine as positive pairs,
similar-prose / different-wines mined as hard negatives. The result
is [`archisgore/bge-small-winetone`](https://huggingface.co/archisgore/bge-small-winetone),
a 384-dim encoder that knows wine vocabulary natively. The full
corpus is re-encoded against it; the model is the live default. The
per-user calibrated projection still runs on top of this — but from
a starting point that already understands "VA" doesn't mean variable
assignment.

## Negative labels are as much signal as positive

The first version treated every label as "this is what I MEAN by
this wine." But a lot of how people *actually* describe wine is
negative: *"Quilceda Creek: punchy, shallow, no nuance."* The
original loss pulled the projection *toward* the wine being
described — exactly the wrong direction.

The loss is now sign-aware. Each label carries a positive/negative
sentiment marker (👍/👎 in the UI). Positive labels minimize
distance from the wine's coordinates; negative labels use a
margin-based push so the projection moves *away*. One line of math;
one radio button in the UI; a real qualitative jump in what the
system can learn about a palate.

## Users found each other

A wine recommender for a single user is a useful prototype. A wine
recommender for a community is a product.

WineTone now has a one-level follow graph. When you fit your
projection, your own labels weight 1.0 and your followees' labels
weight 0.3 each. A brand-new user who follows two people whose
vocabulary resembles theirs gets useful recommendations from two of
their own labels instead of needing five. Cold-start solved cheaply.

A public [`/users`](https://tone.wine/users) directory shows
everyone with their label counts, sentiment ratios, follower counts,
and calibration status. It's how new users find their tribe.

## The site looks like a product

The first post showed screenshots of a developer-built interface.
You could tell. Today's site is a deliberate visual design pass:
bold Inter typography, a single sticky nav, white surfaces with
warm-gray cards, a burgundy/gold accent palette, generous whitespace.

Three things the redesign enabled along the way:

- **A wine-label scanner.** Open
  [`/wines/scan`](https://tone.wine/wines/scan) on your phone,
  snap a photo of any bottle, and a vision-language model extracts
  producer + wine + vintage + variety + country in ~2 seconds. Match
  against the corpus, or pre-fill a new submission. Easily the
  most-used route now.
- **A catalog browse page.** Filter the 164K wines by country or
  variety, sort by user-label count or recency, click into a
  per-wine detail page showing public reviewer aggregates
  side-by-side with WineTone users' own labels.
  [`/catalog`](https://tone.wine/catalog).
- **Installable as a phone app.** It's a PWA. iOS Safari → Share →
  Add to Home Screen; Android Chrome auto-prompts. Opens full-bleed
  with its own icon. No App Store.

## The infrastructure stopped being a worry

Sentry collects errors, UptimeRobot pages on outages, Cloudflare
Web Analytics counts visits (cookielessly). Neon Postgres handles
point-in-time recovery. The Clerk production instance handles auth
on `clerk.tone.wine`. Alembic manages schema migrations. A two-stage
stage→prod pipeline (`staging.tone.wine` → `tone.wine`) catches
regressions before users see them.

It's no longer "running on my laptop and you should not click
delete."

## What's next

I'm going to resist the temptation to list a roadmap. The honest
summary: now that the floor is solid, the next set of choices is
about *whose vocabulary* the system gets to learn. More users with
diverse palates means a richer global corpus, and the project's
flywheel — label scanner → calibration → recommendations — only
spins if the people show up.

If you've labeled wines on WineTone, tell me what surprised you.
If you haven't yet: [tone.wine](https://tone.wine). Bring five
wines you know. Type honestly.
