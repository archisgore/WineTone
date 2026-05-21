"""Conversational query layer — LLM-translated natural language → existing
recommend() / vocab_search() backends.

The LLM has one job: route. Given a free-form user message
("something for a melancholy autumn night", "what would my friend who
loves tandoor smoke drink?", "show me wines described as syrupy"),
decide which backend search to invoke and produce a query string the
embedding model can do something with.

We deliberately don't have the LLM rank or narrate wines — the
embedding search is already the right tool for that, and adding the
LLM to the result-formatting path would double the latency and the
hallucination surface. The LLM is the *router and translator*, not
the ranker.

Inference runs via HF Inference Providers using the same HF token the
Space already has. Pro tier includes monthly credits. If inference
fails for any reason — network, quota, malformed JSON, model down —
we fall back to passing the raw query straight to `recommend()`, so
the user always gets a result.
"""

from __future__ import annotations

import json
import logging
import os
import re

import pandas as pd
from sqlalchemy import text

from winetone import db

log = logging.getLogger(__name__)


# Default model. Llama-3.1-8B is the most reliably-served instruct
# model on HF Inference Providers as of 2026. Override via env if a
# bigger / smaller model behaves better on a given query distribution.
DEFAULT_MODEL = os.environ.get(
    "WINETONE_LLM_MODEL", "meta-llama/Llama-3.1-8B-Instruct"
)

SYSTEM_PROMPT = """You are a wine recommendation router for WineTone.

Your job is to read a user's free-form question and decide which
backend to send it to. You output a small JSON object — nothing else.

Three backends:

1. "recommend" — embedding-based wine search over the descriptors
   wine reviewers use. Use this for ANY question about flavor, mood,
   occasion, weather, food pairing, region, or grape.

   CRITICAL: for mood / occasion / weather / pairing queries you MUST
   translate the user's words into concrete flavor descriptors a wine
   reviewer would write. Don't pass mood words through unchanged.

   Examples:
   - "melancholy rainy autumn evening" →
       "earthy contemplative forest floor mushroom slow finish dim ruby"
   - "celebration for 10th anniversary" →
       "elegant fine bubbles brioche citrus long finish toast"
   - "pairs with mushroom risotto" →
       "earthy umami medium-bodied light tannin mushroom forest floor"
   - "tandoor smoke" → "smoky charred grilled tobacco leather dark fruit"

   Keep translations under 14 words, all flavor/aroma/structure terms.

2. "vocab_search" — a public corpus of OTHER users' personal tasting
   notes. Use ONLY when the user explicitly asks to find wines
   someone described a particular way ("show me wines described as
   syrupy", "what did people call sunshine in a bottle"). Pass the
   user's phrase verbatim — DO NOT translate to flavor language.

3. "alternative_to" — find a wine like a SPECIFIC named wine, often
   cheaper. Use when the user names a real wine/producer and asks for
   "something like it", "cheaper alternative", "similar but under $X",
   "the affordable version of X", etc. Put the named wine in the
   "reference" field, the budget in "max_price" if given.

   Examples:
   - "find me something like Pétrus but under $100" →
       {"intent":"alternative_to","reference":"Petrus","max_price":100}
   - "the affordable version of Caymus" →
       {"intent":"alternative_to","reference":"Caymus"}
   - "a cheaper Sassicaia" →
       {"intent":"alternative_to","reference":"Sassicaia"}

PRICE EXTRACTION (works for any intent):
If the user states an explicit budget or price-tier vocabulary, set
"max_price" (USD). Recognize named price tiers:

  - "Two Buck Chuck" / "Charles Shaw"   → max_price: 10
  - "Yellow Tail level" / "Barefoot"     → max_price: 15
  - "house wine" / "bottom shelf"        → max_price: 20
  - "everyday" / "Tuesday wine"          → max_price: 30
  - "midrange" / "weekend"               → max_price: 50
  - "splurge" / "anniversary"            → min_price: 75
  - explicit "$X" or "under $X" or "around $X" → max_price: X
  - "between $X and $Y"                  → min_price: X, max_price: Y

About any user-context provided: it's vocabulary grounding, not a
preference signal. Don't apply the user's previous-label words to a
new query unless the new query is semantically related.

Output exactly this JSON shape (omit unused keys), nothing else:
{"intent": "recommend" | "vocab_search" | "alternative_to",
 "query": "<for recommend / vocab_search>",
 "reference": "<for alternative_to: the named wine>",
 "max_price": <number, USD, optional>,
 "min_price": <number, USD, optional>,
 "interpretation": "<one sentence in second person, 'I read your question as...'>"}
"""


def _sample_corpus_labels(n: int = 5) -> list[str]:
    """A few random user-written descriptions to give the LLM
    grounding in what the corpus vocabulary actually looks like."""
    try:
        df = pd.read_sql(
            text(
                "SELECT description FROM user_labels "
                "WHERE LENGTH(description) > 30 "
                "ORDER BY RANDOM() LIMIT :n"
            ),
            db.engine(), params={"n": n},
        )
        return df["description"].tolist()
    except Exception:  # noqa: BLE001
        return []


def _user_labels(user_id: str | None, n: int = 4) -> list[str]:
    if not user_id:
        return []
    try:
        df = pd.read_sql(
            text(
                "SELECT description FROM user_labels "
                "WHERE user_id = :u LIMIT :n"
            ),
            db.engine(), params={"u": user_id, "n": n},
        )
        return df["description"].tolist()
    except Exception:  # noqa: BLE001
        return []


def _extract_json(text_blob: str) -> dict | None:
    """LLMs sometimes wrap JSON in prose or code fences. Be tolerant."""
    text_blob = text_blob.strip()
    # Try a direct parse first.
    try:
        return json.loads(text_blob)
    except json.JSONDecodeError:
        pass
    # Fall back to extracting the first {...} block.
    m = re.search(r"\{[^{}]*\}", text_blob, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


def route(query: str, user_id: str | None = None) -> dict:
    """Run the LLM router. Always returns a dict with intent/query/
    interpretation, falling back to a plain recommend if the LLM
    is unreachable or its output won't parse.

    Returned dict:
      {"intent": "recommend" | "vocab_search",
       "query": str,
       "interpretation": str,
       "fallback": bool}      # True if LLM failed and we defaulted
    """
    fallback = {
        "intent": "recommend",
        "query": query,
        "reference": "",
        "max_price": None,
        "min_price": None,
        "interpretation": (
            "(The conversational router is unavailable right now — "
            "running your question as a direct wine search.)"
        ),
        "fallback": True,
    }

    try:
        from huggingface_hub import InferenceClient
    except ImportError:
        log.warning("huggingface_hub not installed; LLM router falling back")
        return fallback

    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if not token:
        log.info("no HF_TOKEN in env; LLM router falling back")
        return fallback

    you = _user_labels(user_id)
    others = _sample_corpus_labels()
    user_msg_parts = [f"User question: {query!r}"]
    if you:
        user_msg_parts.append(
            "User has previously written these labels: "
            + " | ".join(f"{d!r}" for d in you)
        )
    if others:
        user_msg_parts.append(
            "Examples of labels other users have written: "
            + " | ".join(f"{d!r}" for d in others)
        )

    try:
        client = InferenceClient(token=token, timeout=20)
        completion = client.chat_completion(
            model=DEFAULT_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "\n".join(user_msg_parts)},
            ],
            max_tokens=200,
            temperature=0.2,
        )
        raw = completion.choices[0].message.content or ""
    except Exception as e:  # noqa: BLE001
        log.warning("LLM call failed: %s", e)
        return fallback

    parsed = _extract_json(raw)
    if not parsed:
        log.warning("LLM did not return JSON; falling back. raw=%r", raw[:200])
        return fallback

    intent = parsed.get("intent")
    if intent not in ("recommend", "vocab_search", "alternative_to"):
        intent = "recommend"

    def _num(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "intent": intent,
        "query": (parsed.get("query") or query).strip(),
        "reference": (parsed.get("reference") or "").strip(),
        "max_price": _num(parsed.get("max_price")),
        "min_price": _num(parsed.get("min_price")),
        "interpretation": (parsed.get("interpretation") or "").strip(),
        "fallback": False,
    }


# ----------------------------------------------------------------------
# Narrator — second LLM pass that explains the retrieved results in
# natural language (with markdown tables when the user asked for one).
# ----------------------------------------------------------------------

NARRATOR_PROMPT = """You are WineTone's narrator. A user asked a wine question,
WineTone's search engine has returned concrete results, and your job is to
write the conversational answer.

What you have access to:
- The user's question.
- The router's interpretation of that question.
- A DATA block listing each wine's producer, wine name, vintage,
  variety, region/country, cosine similarity to the reference (when
  applicable), price (when known), and % savings (when applicable).

What you DO NOT have:
- Flavor descriptions of any specific wine. Do not write descriptions
  like "buttery" or "earthy with cherry notes" — those are not in
  your data. Stick to facts present in the DATA block.
- Critic scores, reviews, food pairings beyond what's in the DATA.

Rules:
- Answer the user's question directly, using ONLY the DATA provided.
  Never invent producers, prices, scores, descriptions, or wines that
  aren't in the list.
- Be concise — 2-4 short paragraphs at most. No throat-clearing.
- Use Markdown. Use Markdown tables when the user asked for a table
  or a comparison ("show me X next to Y", "with a column for Z").
- Table columns must be things in the DATA: wine name, variety,
  region, vintage, price, similarity, savings, flavor distance. Do
  NOT add a "description" or "tasting notes" column.
- When the user asked for "flavor distance", compute it as
  1 − cosine similarity. Lower = more similar.
- Don't list every wine if there are many — pick the most relevant
  3-6 and reference "(plus N more below)" if the structured table
  will show the rest.
- Skip wine-snob vocabulary unless it earns its place. The user is
  curious but not necessarily an expert.
- Don't repeat the user's question back at them. Don't end with
  "Let me know if you have other questions" — they already know.
"""


def _vintage_str(v: object) -> str:
    """Render a vintage field defensively — pandas floats with NaN, None,
    and ints all coexist in the rowdicts depending on which backend
    populated them."""
    import math
    if v is None:
        return "NV"
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return "NV"
    if math.isnan(fv):
        return "NV"
    return str(int(fv))


def _price_str(p: object) -> str:
    """Same defensiveness for median_price."""
    import math
    if p is None:
        return "?"
    try:
        fp = float(p)
    except (TypeError, ValueError):
        return "?"
    if math.isnan(fp):
        return "?"
    return f"${int(fp)}"


def _format_results_for_narrator(
    intent: str,
    results: dict,
) -> str:
    """Compact textual rendering of the search results for the LLM."""
    lines: list[str] = []

    if intent == "alternative_to":
        ref = results.get("reference")
        if ref:
            ref_price = ref.get("median_price")
            lines.append(
                f"Reference wine: {ref.get('producer_display', '?')} "
                f"{ref.get('wine_display') or ''} "
                f"({_vintage_str(ref.get('vintage'))}, "
                f"{ref.get('country', '?')}). "
                f"Price: {_price_str(ref_price)}."
            )
        rows = results.get("rows") or []
        lines.append(f"\nAlternatives (cosine similarity to the reference, "
                     f"price, % savings vs reference):")
        for i, r in enumerate(rows, 1):
            sim = r.get("similarity", 0)
            sav = r.get("savings")
            sav_str = f"{int(sav*100):+d}%" if sav is not None else "—"
            lines.append(
                f"  {i}. {r.get('producer_display', '?')} "
                f"{(r.get('wine_display') or '')[:40]} "
                f"({_vintage_str(r.get('vintage'))}, "
                f"{r.get('variety') or ''}, "
                f"{r.get('region') or r.get('country') or ''}) — "
                f"cosine {sim:.3f}, {_price_str(r.get('median_price'))}, "
                f"savings {sav_str}"
            )
    elif intent == "vocab_search":
        rows = results.get("rows") or []
        lines.append(f"Wines matched by vocabulary (similarity, the actual "
                     f"description that matched, who wrote it):")
        for i, r in enumerate(rows, 1):
            lines.append(
                f"  {i}. {r.get('producer_display', '?')} "
                f"{(r.get('wine_display') or '')[:40]} — "
                f"cosine {r.get('similarity', 0):.3f}, "
                f"described as \"{r.get('description', '')[:90]}\" "
                f"by {r.get('user_display_name', '?')}"
            )
    else:  # recommend
        rows = results.get("rows") or []
        lines.append(f"Top wines by hybrid score (dense + sparse), "
                     f"price, country/region/variety:")
        for i, r in enumerate(rows, 1):
            lines.append(
                f"  {i}. {r.get('producer_display', '?')} "
                f"{(r.get('wine_display') or '')[:40]} "
                f"({_vintage_str(r.get('vintage'))}, "
                f"{r.get('variety') or ''}, "
                f"{r.get('region') or r.get('country') or ''}) — "
                f"score {r.get('similarity', 0):.3f}, "
                f"{_price_str(r.get('median_price'))}"
            )

    return "\n".join(lines)


def narrate(
    query: str,
    intent: str,
    results: dict,
    interpretation: str = "",
) -> str:
    """Run the narrator LLM pass; return Markdown to render to HTML.

    `results` shape:
        {
          "rows": [list of dicts — recommend / alternatives / vocab rows],
          "reference": dict | None,  # for alternative_to
        }

    Returns empty string if the LLM isn't reachable — the caller should
    treat that as "no narration, show the structured table only".
    """
    try:
        from huggingface_hub import InferenceClient
    except ImportError:
        return ""

    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if not token:
        return ""

    body = _format_results_for_narrator(intent, results)
    if not body:
        return ""

    user_msg = (
        f"User question: {query!r}\n\n"
        f"Routing interpretation: {interpretation}\n\n"
        f"Search backend: {intent}\n\n"
        f"Data:\n{body}"
    )

    try:
        client = InferenceClient(token=token, timeout=30)
        completion = client.chat_completion(
            model=DEFAULT_MODEL,
            messages=[
                {"role": "system", "content": NARRATOR_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=600,
            temperature=0.3,
        )
        return (completion.choices[0].message.content or "").strip()
    except Exception as e:  # noqa: BLE001
        log.warning("narrator call failed: %s", e)
        return ""
