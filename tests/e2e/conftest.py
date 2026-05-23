"""End-to-end test fixtures.

The target URL comes from the WINETONE_E2E_URL env var; defaults to
the production site for local runs. CI sets it explicitly per
workflow invocation.

The suite is anonymous-only in v1 — see docs/runbooks/e2e-testing.md
for the authenticated-flow follow-up.
"""

from __future__ import annotations

import os

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--base-url",
        action="store",
        default=os.environ.get("WINETONE_E2E_URL", "https://tone.wine"),
        help="Base URL to test against (defaults to WINETONE_E2E_URL or https://tone.wine)",
    )


@pytest.fixture(scope="session")
def base_url(pytestconfig) -> str:
    url = pytestconfig.getoption("--base-url").rstrip("/")
    return url


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args, base_url):
    """Tell Playwright pages to use the target URL as their base."""
    return {**browser_context_args, "base_url": base_url}
