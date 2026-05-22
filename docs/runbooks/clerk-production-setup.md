# Runbook: Promoting Clerk from Development → Production

*Estimated time: 20 minutes elapsed, ~5 minutes hands-on.*

Today WineTone is running on Clerk **test** keys
(`pk_test_*` / `sk_test_*`). Clerk's sign-in modal shows a small
"Development mode" banner, sessions are bound to the
`united-stork-42.clerk.accounts.dev` domain, and *every* sign-up
shows up in the development instance's user list.

Production removes the banner, binds sessions to `tone.wine`, and
isolates real users from anything we've tested with.

---

## Steps

### 1. Create the Production Instance in Clerk

1. Open <https://dashboard.clerk.com>.
2. Top-left, click the instance switcher → **Create production instance**.
3. Name it `WineTone (production)`. Select the same auth methods as
   development (email magic link, Google, GitHub).

### 2. Bind the production instance to `tone.wine`

1. In the production instance dashboard, go to **Customization → Domains**.
2. Add `tone.wine` as the production domain.
3. Clerk will give you 4-5 DNS records to add — most importantly:
   - `CNAME accounts.tone.wine` → `something.clerk.accounts.dev`
   - Two TXT records for domain-ownership verification
   - One CNAME for clkmail.tone.wine (transactional email delivery)
4. Add each in Cloudflare DNS. **Set proxy status to "DNS only" (grey
   cloud), NOT proxied.** Clerk terminates TLS for the accounts
   subdomain on their own infrastructure and the proxied flow would
   break the cert chain.
5. Wait for Clerk's verification step to flip to "Verified" — usually
   under a minute. If it stalls, check the CNAME with `dig +short
   accounts.tone.wine` from your local machine.

### 3. Copy the production keys

1. **API Keys** sidebar in the production instance.
2. Copy the `pk_live_*` publishable key and the `sk_live_*` secret key.
3. **Do not paste them into chat or anywhere they could leak** — the
   secret key especially. See the deploy log for how the test
   secret leaked into the conversation that birthed the original
   deployment.

### 4. Set them as Space secrets

In a terminal where you have `huggingface_hub` and an HF token logged
in (`~/.cache/huggingface/token`):

```bash
PK_LIVE='pk_live_xxxxxxxxxxxxxxxxxxxxxxx'  # the publishable key
SK_LIVE='sk_live_xxxxxxxxxxxxxxxxxxxxxxx'  # the secret key

cd /Users/archisgore/github/archisgore/WineTone
.venv/bin/python <<PYEOF
import os
from huggingface_hub import HfApi
api = HfApi()
api.add_space_secret("archisgore/winetone",
    "CLERK_PUBLISHABLE_KEY",
    os.environ["PK_LIVE"],
    description="Clerk publishable key (production).")
api.add_space_secret("archisgore/winetone",
    "CLERK_SECRET_KEY",
    os.environ["SK_LIVE"],
    description="Clerk secret key (production).")
api.restart_space("archisgore/winetone", factory_reboot=True)
print("secrets updated; rebuild triggered")
PYEOF
```

### 4b. Configure your own OAuth credentials for each social provider

**This step is non-obvious and easy to miss — it cost a debug cycle.**

Clerk **test** instances ship with Clerk's own shared development
OAuth credentials, so Google / GitHub sign-in "just works" out of
the box. Clerk **production** instances do NOT — every production
instance must use your own OAuth app per provider. If you forget
this step and click "Sign in with Google" on the production site,
Google rejects with:

```
Access blocked: Authorization Error
Missing required parameter: client_id
Error 400: invalid_request
```

The fix is to create your own OAuth app at each provider and paste
the credentials into Clerk's social-connection config.

#### Google

1. In the **Clerk** production dashboard → **User & Authentication →
   Social Connections → Google**. Note the two values Clerk displays:
   - **Authorized JavaScript origin** (something like `https://clerk.tone.wine`)
   - **Authorized redirect URI** (something like
     `https://clerk.tone.wine/v1/oauth_callback`)

   These are the exact strings Google needs — don't paraphrase them.

2. <https://console.cloud.google.com> → create / select a project
   (e.g., "WineTone Production").

3. **APIs & Services → OAuth consent screen**:
   - User type: **External**
   - App name: WineTone
   - Support email + Developer contact: me@archisgore.com
   - Authorized domains: `tone.wine`
   - Scopes: `openid`, `email`, `profile`
   - **Click PUBLISH APP at the top of the consent-screen page**, or
     only manually-added test users can sign in.

4. **APIs & Services → Credentials → + Create Credentials → OAuth client ID**:
   - Application type: Web application
   - Name: "Clerk production"
   - Authorized JavaScript origins: *paste from Clerk*
   - Authorized redirect URIs: *paste from Clerk*
   - Create — Google returns a `client_id` (`*.apps.googleusercontent.com`)
     and a `client_secret`.

5. Back in **Clerk** → Google connection → toggle **Use custom
   credentials** → paste `client_id` + `client_secret` → save.

No Space restart needed — Clerk picks up the change immediately on
the next sign-in attempt.

#### GitHub (and any other social provider)

Same shape:

1. GitHub → **Settings → Developer settings → OAuth Apps → New OAuth App**.
2. Homepage URL: `https://tone.wine`. Authorization callback URL:
   the one Clerk shows in its GitHub social-connection page.
3. Register → grab the client_id + generate a client_secret.
4. Paste into Clerk's GitHub social-connection settings.

#### Email magic-link

No setup needed here. Clerk runs the email magic-link flow on their
own infra — the only thing it depends on is the
`clkmail.tone.wine` CNAME, which step 2 already added.

### 5. Configure the user-deletion webhook

1. Production instance dashboard → **Webhooks → Add Endpoint**.
2. URL: `https://tone.wine/webhooks/clerk`.
3. Subscribe to the `user.deleted` event. Optionally also
   `user.updated` if we want to sync username changes (not currently
   handled, but harmless to subscribe).
4. Copy the **Signing Secret** (starts with `whsec_*`).
5. Add it as a Space secret named `CLERK_WEBHOOK_SECRET`:

```bash
SK_WEBHOOK='whsec_xxxxxxxxxxxxxxxxx'
cd /Users/archisgore/github/archisgore/WineTone
WS="$SK_WEBHOOK" .venv/bin/python -c "
import os
from huggingface_hub import HfApi
HfApi().add_space_secret('archisgore/winetone',
    'CLERK_WEBHOOK_SECRET', os.environ['WS'],
    description='Clerk webhook signing secret (svix-verified)')
HfApi().restart_space('archisgore/winetone', factory_reboot=False)
"
```

`factory_reboot=False` here because we only need the app to re-read
its env vars; we don't need a full image rebuild.

### 6. Verify

After the rebuild settles (~3 min):

1. Open <https://tone.wine> in an incognito window.
2. Click **Sign in**. The Clerk modal should appear without the
   "Development mode" banner.
3. Sign up with a throwaway email.
4. From within the user dropdown, click **Delete account**. Confirm
   the prompt.
5. Run `curl -s https://tone.wine/u/<that-username>` — should return
   `404 No such user`, confirming the webhook fired and the local
   DELETE cascaded.

### 7. Rotate the development keys

Once production is verified working:

1. Delete the `pk_test_*` / `sk_test_*` keys in Clerk's development
   instance dashboard — they leaked into a chat transcript and are
   technically compromised.
2. Optionally archive the development instance entirely. Sessions
   created there don't carry over to production.

---

## Rollback

If something goes wrong at any step before step 6, the rollback is:

```bash
PK_TEST='pk_test_dW5pdGVkLXN0b3JrLTQyLmNsZXJrLmFjY291bnRzLmRldiQ'
SK_TEST='<old test secret>'
cd /Users/archisgore/github/archisgore/WineTone
.venv/bin/python -c "
import os
from huggingface_hub import HfApi
api = HfApi()
api.add_space_secret('archisgore/winetone', 'CLERK_PUBLISHABLE_KEY', os.environ['PK_TEST'])
api.add_space_secret('archisgore/winetone', 'CLERK_SECRET_KEY', os.environ['SK_TEST'])
api.restart_space('archisgore/winetone', factory_reboot=True)
"
```

Test keys never expire on their own and the development instance
won't have been deleted yet at this stage, so this gets you back to
the previous working state in ~4 min.
