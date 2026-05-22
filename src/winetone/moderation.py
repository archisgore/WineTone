"""Minimum-viable content moderation for user-submitted text.

What this is: a tripwire layer that flags obvious garbage (URLs in
descriptions, all-caps shouting, a few category-spam patterns) and
reports flagged content to Sentry so we see it on a daily basis.
NOT a real moderation system — no ML classifier, no abuse-report
queue, no human review UI.

What this is not: a content gate. Flagging does NOT block submission.
The user-vocabulary thesis lives or dies on the diversity of language
people use to describe wines; we cannot pre-judge "buttery" as
acceptable and "thicc" as not. We log the flag, surface it to me,
and decide reactively if there's a pattern worth blocking.

Two callers:
  - recommend.add_label(...) — every wine label
  - submit.submit_wine(...) — every wine submission's free-text fields

Both call moderation.screen(text, kind=...) and act on what it returns:
the original text (always) + a list of flags. Caller decides what to
do with flags. Today: log + Sentry. Tomorrow maybe: block on certain
high-confidence categories.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)


_RULES: list[tuple[str, re.Pattern[str], str]] = [
    # (flag_id, regex, human-readable description)
    ("url", re.compile(r"https?://|www\.", re.IGNORECASE),
     "Contains a URL"),
    ("phone_number", re.compile(r"\+?\d[\d\s\-]{7,}"),
     "Looks like a phone number"),
    ("email_address", re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
     "Contains an email address"),
    ("all_caps_shouting", re.compile(r"\b[A-Z]{6,}\b"),
     "Contains long all-caps run (≥6 chars)"),
    ("repeated_chars", re.compile(r"(.)\1{8,}"),
     "Contains 8+ of the same character in a row"),
    # Crypto / casino spam — extremely common in open user-content fields.
    ("crypto_spam", re.compile(
        r"\b(bitcoin|btc|crypto|airdrop|metamask|wallet)\b", re.IGNORECASE),
     "Contains crypto-spam keywords"),
    ("casino_spam", re.compile(
        r"\b(casino|gambling|poker|slots|jackpot|betting)\b", re.IGNORECASE),
     "Contains casino-spam keywords"),
    # Trying to look like markup injection — Jinja autoescape covers
    # this on the render side, but flag noisy attempts.
    ("script_tag", re.compile(r"<\s*script", re.IGNORECASE),
     "Contains a <script> tag attempt"),
]


@dataclass
class Flag:
    flag_id: str
    description: str

    def __str__(self) -> str:
        return f"{self.flag_id}: {self.description}"


def screen(text: str, *, kind: str = "label") -> list[Flag]:
    """Return any moderation flags for `text`. Empty list = clean.

    `kind` is included in the Sentry breadcrumb so we can tell
    label-flags from wine-submission-flags later.
    """
    if not text:
        return []
    flags = [Flag(fid, desc) for fid, pat, desc in _RULES if pat.search(text)]
    if not flags:
        return []
    log.warning(
        "moderation flagged %s text (%d flags): %s · text[:80]=%r",
        kind, len(flags), ", ".join(f.flag_id for f in flags), text[:80],
    )
    _report_to_sentry(text, kind, flags)
    return flags


def _report_to_sentry(text: str, kind: str, flags: list[Flag]) -> None:
    """Fire-and-forget Sentry message. No-op if Sentry isn't configured."""
    if not os.environ.get("SENTRY_DSN"):
        return
    try:
        import sentry_sdk
        with sentry_sdk.push_scope() as scope:
            scope.set_tag("moderation", "flagged")
            scope.set_tag("moderation_kind", kind)
            for f in flags:
                scope.set_tag(f"moderation_flag_{f.flag_id}", "true")
            scope.set_extra("text_preview", text[:500])
            sentry_sdk.capture_message(
                f"Moderation flagged {kind}: {', '.join(f.flag_id for f in flags)}",
                level="warning",
            )
    except Exception:  # noqa: BLE001
        pass  # never fail the request because moderation breadcrumbing broke
