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

        # --- Verify 1: server-side ---
        # /me should redirect to /u/<display_name> when the server sees
        # us as signed in. If it returns to landing, the cookies aren't
        # being read.
        print()
        print(f"  [1/2] Server-side verify via {URL}/me …")
        page.goto(f"{URL}/me")
        final_url = page.url.rstrip("/")
        print(f"        landed at: {final_url}")
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
            print("    user's display name doesn't appear to be")
            print(f"    '{EXPECTED_USERNAME}'. Fix in Clerk dashboard or")
            print("    on the /me page itself, then re-run.")
            browser.close()
            return 1

        # --- Verify 2: client-side Clerk JS activation ---
        # The e2e tests need Clerk JS to be able to load a session
        # from the captured cookies, not just the server. If the
        # capture missed the `client-uat` cookie or related Clerk
        # client state, Clerk.session is null and the tests fail at
        # the signed_in_page fixture. Reproduce that exact check here
        # so we never save a state that can't be re-activated.
        print()
        print("  [2/2] Client-side Clerk JS activation …")
        page.goto(f"{URL}/")
        try:
            page.wait_for_function(
                "() => typeof window.Clerk !== 'undefined'", timeout=15_000,
            )
            token = page.evaluate(
                """async () => {
                    await window.Clerk.load();
                    if (!window.Clerk.session) return null;
                    return await window.Clerk.session.getToken({ skipCache: true });
                }"""
            )
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ clerk-js failed to load within 15s: {e}")
            browser.close()
            return 1
        if not token:
            print()
            print("  ✗ Server says you're signed in, but Clerk JS can't")
            print("    activate a session from the current state. The")
            print("    `client-uat` cookie or its sibling client-side")
            print("    state is missing — likely because the snapshot")
            print("    was taken before clerk-js finished writing it.")
            print()
            print("    Try one of:")
            print("      • Click around the visible window for ~10 seconds")
            print("        after the Clerk modal closes, then return here.")
            print("      • Open /me in the visible window first — verify")
            print("        the Clerk user button (top-right) is rendered.")
            print("        THEN press Enter.")
            browser.close()
            return 1
        print(f"        ✓ Clerk JS minted a token (length {len(token)})")

        ctx.storage_state(path=OUT)
        print()
        print(f"  ✓ Session verified (server + Clerk JS). Wrote {OUT}.")
        print("  Upload with:")
        print(f"    gh secret set E2E_STAGING_AUTH_STATE < {OUT} \\")
        print("       --env production --repo archisgore/WineTone")
        browser.close()
        return 0


if __name__ == "__main__":
    sys.exit(main())
