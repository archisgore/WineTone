"""Capture a Playwright storage_state for the e2e test account.

Opens a visible Chromium window pointed at staging.tone.wine; you
sign in manually (Clerk magic-link or OAuth); when the session is
live, press Enter back in the terminal and the captured cookies +
localStorage are written to auth.json.

Then store the result as a GitHub Actions secret:

    gh secret set E2E_STAGING_AUTH_STATE < auth.json

Full procedure: docs/runbooks/e2e-testing.md
"""

from __future__ import annotations

from playwright.sync_api import sync_playwright

URL = "https://staging.tone.wine"
OUT = "auth.json"


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(URL)
        print()
        print(f"  Sign in to the staging Clerk instance at {URL}.")
        print("  Set your display name to 'e2e-test' if you haven't already.")
        print("  Once signed in (test_me_resolves_to_signed_in_dashboard")
        print("  needs /me to redirect to /u/e2e-test), come back here.")
        print()
        input("  Press Enter to capture the session… ")
        ctx.storage_state(path=OUT)
        print(f"\n  Wrote {OUT}. Upload with:")
        print(f"    gh secret set E2E_STAGING_AUTH_STATE < {OUT}")
        browser.close()


if __name__ == "__main__":
    main()
