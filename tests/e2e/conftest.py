"""End-to-end test fixtures.

`app_url` is the URL to test against. CI sets it via WINETONE_E2E_URL
in the workflow; local runs default to the production site. Pass
`--target=https://staging.tone.wine` on the command line to override.

The suite is anonymous-only in v1 — see docs/runbooks/e2e-testing.md
for the authenticated-flow follow-up.
"""

from __future__ import annotations

import os

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--target",
        action="store",
        default=None,
        help="Base URL to test against. Falls back to WINETONE_E2E_URL "
             "env var, then to https://tone.wine.",
    )


@pytest.fixture(scope="session")
def app_url(pytestconfig) -> str:
    """Resolve the target URL — CLI flag wins, then env var, then prod."""
    url = (
        pytestconfig.getoption("--target")
        or os.environ.get("WINETONE_E2E_URL")
        or "https://tone.wine"
    )
    return url.rstrip("/")


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args, app_url):
    """Tell Playwright to use the target URL as the default base for
    relative `page.goto('/foo')` calls — though we always pass
    absolute URLs in tests for clarity, so this is belt-and-suspenders.
    """
    return {**browser_context_args, "base_url": app_url}
