"""Capture a Playwright storage_state for the e2e test account.

Opens a visible Chromium window pointed at staging.tone.wine; you
sign in manually (Clerk magic-link or OAuth); the script then
verifies the session is actually live before saving auth.json.

Verification: after you press Enter, the script navigates to /me.
A signed-in browser gets redirected to /u/<display-name>. An
anonymous browser gets redirected to /. If we land at / we refuse
to write the file and ask you to try again — that's how we caught
the silent-anonymous-capture bug on first try.

Then store the verified result as a GitHub Actions secret:

    gh secret set E2E_STAGING_AUTH_STATE < auth.json

Full procedure: docs/runbooks/e2e-testing.md
"""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

URL = "https://staging.tone.wine"
OUT = "auth.json"
EXPECTED_USERNAME = "e2e-test"


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(URL)
        print()
        print(f"  Sign in to the staging Clerk instance at {URL}.")
        print(f"  Display name must be '{EXPECTED_USERNAME}'.")
        print()
        print("  Tip: after the Clerk modal closes, click around long")
        print("  enough that you're sure the session is live. Visit /me")
        print("  in the window — if it redirects to /u/e2e-test you're")
        print("  good. Then come back here.")
        print()
        input("  Press Enter to verify and capture the session… ")

        # Verify the session is actually live before saving.
        print()
        print(f"  Verifying by navigating to {URL}/me …")
        page.goto(f"{URL}/me")
        final_url = page.url.rstrip("/")
        print(f"  /me landed at: {final_url}")
        if final_url == URL or final_url == URL.rstrip("/"):
            print()
            print("  ✗ /me redirected to the landing page — the browser")
            print("    is NOT signed in. Try again: re-open the Clerk")
            print("    sign-in modal in the visible window, complete it,")
            print("    verify by clicking around, and re-run this script.")
            browser.close()
            return 1
        if f"/u/{EXPECTED_USERNAME}" not in final_url and "/age-gate" not in final_url:
            print()
            print(f"  ✗ /me redirected to {final_url} — expected")
            print(f"    /u/{EXPECTED_USERNAME} or /age-gate. The signed-in")
            print(f"    user's display name doesn't appear to be")
            print(f"    '{EXPECTED_USERNAME}'. Fix in Clerk dashboard or")
            print(f"    on the /me page itself, then re-run.")
            browser.close()
            return 1

        ctx.storage_state(path=OUT)
        print()
        print(f"  ✓ Session verified. Wrote {OUT}.")
        print(f"  Upload with:")
        print(f"    gh secret set E2E_STAGING_AUTH_STATE < {OUT}")
        browser.close()
        return 0


if __name__ == "__main__":
    sys.exit(main())
