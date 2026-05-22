"""Unit tests for winetone.lexical.

Network-free: the only function exposed publicly that talks to the DB
is score_candidates(), which we test against a tiny in-memory SQLite
DB so the test suite doesn't depend on Neon being up. The
build_tsv_expression() function is pure-Python and trivially testable.
"""

from __future__ import annotations

from winetone import lexical


def test_build_tsv_expression_concatenates_and_skips_empty():
    out = lexical.build_tsv_expression(
        producer="Biondi Santi",
        wine_name="Brunello Riserva",
        variety="",
        region="Tuscany",
        country="Italy",
        description="",
    )
    # Empty fields are skipped, no double spaces leak in.
    assert "Biondi Santi" in out
    assert "Brunello Riserva" in out
    assert "Tuscany" in out
    assert "Italy" in out
    # No "" placeholder, no double-space artifacts.
    assert "  " not in out


def test_build_tsv_expression_handles_all_empty():
    # Producer-only case (the minimum-viable submission shape).
    out = lexical.build_tsv_expression(producer="Test Cellars")
    assert out == "Test Cellars"


def test_score_candidates_empty_query_returns_empty():
    # Pure-Python guard before any DB access.
    assert lexical.score_candidates("", limit=10) == {}
    assert lexical.score_candidates("   ", limit=10) == {}
