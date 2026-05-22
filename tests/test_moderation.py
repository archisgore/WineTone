"""Tests for winetone.moderation."""

from __future__ import annotations

from winetone import moderation


def test_clean_text_returns_no_flags():
    assert moderation.screen(
        "buttery oaky chardonnay with vanilla finish",
        kind="label",
    ) == []


def test_url_is_flagged():
    flags = moderation.screen("Buy at https://example.com")
    assert any(f.flag_id == "url" for f in flags)


def test_phone_number_is_flagged():
    flags = moderation.screen("Call me at +1-555-123-4567 for info")
    assert any(f.flag_id == "phone_number" for f in flags)


def test_email_is_flagged():
    flags = moderation.screen("Contact wine@example.com please")
    assert any(f.flag_id == "email_address" for f in flags)


def test_crypto_spam_is_flagged():
    flags = moderation.screen("FREE BITCOIN AIRDROP claim now")
    flag_ids = {f.flag_id for f in flags}
    # Both the crypto-spam keyword AND the all-caps run trip.
    assert "crypto_spam" in flag_ids
    assert "all_caps_shouting" in flag_ids


def test_empty_text_returns_no_flags():
    assert moderation.screen("", kind="label") == []
    assert moderation.screen("   ", kind="label") == []


def test_script_tag_injection_is_flagged():
    flags = moderation.screen("<script>alert('x')</script>")
    assert any(f.flag_id == "script_tag" for f in flags)
