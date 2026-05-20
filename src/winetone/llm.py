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

Two backends:

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
   - "what should I drink with a steak" →
       "full-bodied tannic dark fruit cedar leather long finish"

   Keep translations under 14 words, all flavor/aroma/structure terms.

2. "vocab_search" — a public corpus of OTHER users' personal tasting
   notes. Use ONLY when the user explicitly asks to find wines
   someone described a particular way ("show me wines described as
   syrupy", "what did people call sunshine in a bottle"). Pass the
   user's phrase verbatim — DO NOT translate to flavor language.

About any user-context provided: it's vocabulary grounding, not a
preference signal. Don't apply the user's previous-label words to a
new query unless the new query is semantically related.

Output exactly this JSON shape, nothing else:
{"intent": "recommend" | "vocab_search",
 "query": "<the query to send the backend>",
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
    qstr = (parsed.get("query") or query).strip()
    interp = (parsed.get("interpretation") or "").strip()
    if intent not in ("recommend", "vocab_search"):
        intent = "recommend"
    return {
        "intent": intent,
        "query": qstr,
        "interpretation": interp,
        "fallback": False,
    }
