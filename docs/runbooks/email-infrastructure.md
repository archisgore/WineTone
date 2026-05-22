# Runbook: Transactional Email Infrastructure (planning doc)

*Status: **not yet wired in.** This document is the design we'd
adopt the moment a real product event requires sending mail. It
exists so that when that day comes, the call doesn't take a week
of investigation.*

WineTone currently sends **zero transactional emails**. The whole
auth flow (sign-up, sign-in, magic-link) is handled by Clerk
internally — Clerk's mail goes out from Clerk's infra, with
Clerk's `From:` address, and we don't see it. Whatever
deliverability problems they have are not our problem to debug.

So the question is: what events *we* would originate that warrant
mail, and what infra would carry them.

---

## What we'd actually send

| Event | Priority | Why we'd send |
|---|---|---|
| **Account deletion confirmation** | Medium | Privacy policy promises full deletion. A short "your data was deleted on 2026-MM-DD" closes the loop and gives the user a paper trail. |
| Abuse-report acknowledgment to reporter | Low-Medium | Currently we just log + Sentry. An "we received your report and will look at it within X days" is the standard pattern. |
| Admin alert when a new abuse report comes in | Low | Sentry already does this for us. Mail would be redundant unless Sentry alerting fails. |
| Welcome email after sign-up | **No** | Clerk already sends one. Doubling up looks spammy. |
| Marketing / newsletter | **No** | We don't have a newsletter and don't plan to. |
| Calibration milestone ("you fitted your 10th label!") | **No** | Engagement-bait. Skip. |

The **only one** worth building day-one infrastructure for is the
account-deletion confirmation. Everything else is nice-to-have.

---

## Provider comparison

Both are good. Choosing one isn't a permanent decision — every
modern transactional email API looks roughly the same (`POST` a
JSON body with `to`, `from`, `subject`, `html`, response is an ID).
Swapping later is ~30 min of code change.

### Resend

- **Free tier:** 3,000 emails / month, 100 / day
- **Pricing above free:** $20/mo for 50K emails (much more than
  we'd ever send for our use case)
- **DX:** Modern (built by an ex-Vercel / GitHub team). Their
  React-email + Python clients are clean. Good debugging UI.
- **DNS work:** SPF + DKIM + DMARC for `tone.wine`. ~10 min in
  Cloudflare DNS once.
- **API:** `resend.Emails.send({...})`, returns an ID.
- **Webhooks for delivery events:** yes.
- **Vibe:** built for indie / small SaaS. Feels right for us.

### Postmark

- **Free tier:** **100 / month**. Stingier than Resend.
- **Pricing above free:** $15/mo for 10K emails
- **DX:** Older, more enterprise-feel. Bulletproof deliverability
  reputation — the killer feature if you actually have a
  deliverability problem.
- **DNS work:** SPF + DKIM + DMARC, same as Resend.
- **API:** REST POST.
- **Webhooks for delivery events:** yes.
- **Vibe:** built for businesses where mail not arriving means
  customer revenue. Overkill for us.

### Recommendation

**Use Resend.** Free tier covers our entire foreseeable volume
(deletion confirmations × a few-hundred users in year one is
nowhere near 3,000/mo). Their Python SDK is two lines to call.
We can switch to Postmark later if we ever hit a real
deliverability wall.

---

## Implementation sketch (when we do wire it in)

### Environment variables

```bash
RESEND_API_KEY=re_xxx           # from https://resend.com/api-keys
WINETONE_FROM_EMAIL=hello@tone.wine
WINETONE_SUPPORT_EMAIL=me@archisgore.com
```

### Where it hooks into the codebase

`src/winetone/web/app.py` → the `/account/delete` handler. After
the local + Clerk cascade-delete succeeds, before returning the
redirect, fire a deletion-confirmation mail:

```python
# Pseudocode — not yet implemented.
from winetone.email import send_deletion_confirmation
send_deletion_confirmation(
    to=user_email,
    deleted_at=datetime.utcnow(),
)
```

The handler should `try/except` the mail call and **log-but-not-
fail** if the mail provider is down. Deletion is more important
than the receipt; we never want a failed email to block the
actual data wipe.

### New module

A small `src/winetone/email.py` wrapper:

```python
import os
import resend
import logging

log = logging.getLogger(__name__)
resend.api_key = os.environ.get("RESEND_API_KEY", "")

def _enabled() -> bool:
    return bool(resend.api_key)

def send_deletion_confirmation(to: str, deleted_at) -> None:
    if not _enabled():
        log.info("email disabled; skipping deletion confirmation to %s", to)
        return
    try:
        resend.Emails.send({
            "from": os.environ["WINETONE_FROM_EMAIL"],
            "to": to,
            "subject": "Your WineTone account has been deleted",
            "html": f"""
              <p>Your WineTone account and all associated data
              were deleted at {deleted_at.isoformat()}Z.</p>
              <p>This was a one-way operation; we cannot restore
              the data. If you didn't request this, contact
              <a href="mailto:{os.environ['WINETONE_SUPPORT_EMAIL']}">
              support</a>.</p>
              <p>Thank you for trying WineTone.</p>
            """,
        })
    except Exception as e:
        log.warning("failed to send deletion confirmation to %s: %s", to, e)
```

### Tests

- Unit test that `_enabled()` returns False without an API key.
- Mock `resend.Emails.send` and assert the right args.
- Add a smoke test that calls `/account/delete` with
  `RESEND_API_KEY` unset → verifies no exception leaks even when
  email is "down."

### DNS setup (the part that's actually annoying)

For mail to land in inboxes instead of spam:

1. **SPF**: TXT on `tone.wine` saying Resend's servers are
   authorized senders. Resend gives you the exact record.
2. **DKIM**: Resend generates a signing key + a CNAME record
   for `<selector>._domainkey.tone.wine` pointing at their
   verifier.
3. **DMARC**: TXT on `_dmarc.tone.wine` declaring our policy.
   Start at `p=none` (monitor-only); flip to `p=quarantine`
   after a couple weeks of clean SPF/DKIM passes.
4. **Verify**: send a test email to a Gmail address you control
   + view "show original" → confirm all three pass.

This is ~30 minutes of one-time work. The cost of skipping it
isn't catastrophic ("your mail goes to spam") but it's
embarrassing if a user goes hunting for the deletion receipt
and can't find it.

---

## What's NOT in scope

- **Inbound email handling.** We don't read mail. `me@archisgore.com`
  is the only support address; it stays a personal Gmail.
- **Templated bulk mail.** No newsletter, no usage-report cadences.
- **Email-based auth.** Clerk handles all auth-related mail.
- **Calendar / iCal invites.** Not applicable.

Keeping the scope this tight is on purpose — every email type we
add is one more thing to monitor, design, write tests for, and
maintain a template for. Account-deletion confirmation is the
*only* thing that adds genuine user trust value over silence.
