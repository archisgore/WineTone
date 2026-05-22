"""Tests for winetone.llm.

Network-free: we test the fallback path (no HF_TOKEN in env) which
returns a deterministic shape, plus the small JSON-extraction helper.
The real LLM call requires HF_TOKEN and network; that's an integration
test for a different suite.
"""

from __future__ import annotations

from winetone import llm


def test_router_fallback_when_no_token(monkeypatch):
    """Without HF_TOKEN, the router returns the fallback shape.

    Caller code can rely on these fields existing regardless of
    upstream availability — that's the contract we promise to the
    /ask endpoint.
    """
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)

    r = llm.route("anything at all")

    assert r["fallback"] is True
    assert r["intent"] == "recommend"   # safe default
    assert r["query"] == "anything at all"
    assert r["reference"] == ""
    assert r["max_price"] is None
    assert r["min_price"] is None
    assert "interpretation" in r


def test_extract_json_handles_clean_input():
    parsed = llm._extract_json('{"intent": "recommend", "query": "x"}')
    assert parsed == {"intent": "recommend", "query": "x"}


def test_extract_json_recovers_from_wrapping_prose():
    # LLMs often pad with chatter even when told not to.
    blob = (
        'Sure! Here\'s your JSON: '
        '{"intent": "recommend", "query": "x"} '
        'Hope that helps!'
    )
    parsed = llm._extract_json(blob)
    assert parsed == {"intent": "recommend", "query": "x"}


def test_extract_json_returns_none_on_garbage():
    assert llm._extract_json("not a json blob at all") is None
    assert llm._extract_json("") is None
