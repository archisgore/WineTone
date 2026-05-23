# Runbook: HF Spaces cutover to private GHCR image

*Created 2026-05-23 alongside the GHCR build pipeline.*

The WineTone GitHub repo is now private. The HF Spaces' previous
Dockerfile relied on a public `git clone` to fetch source at build
time — that path is dead. This runbook is the **cutover** from
source-in-Space to image-pulled-from-private-registry.

After cutover:
- Public Space repos contain only `Dockerfile` (a 3-line `FROM` shim),
  `README.md`, `.gitattributes`. No source.
- All build work happens in `.github/workflows/build-image.yml`,
  pushing to `ghcr.io/archisgore/winetone` (private).
- HF Spaces pull the prebuilt image at runtime, authenticated against
  GHCR via a Space secret holding a GitHub PAT.

---

## Pre-cutover state — what's where

| Surface | Visibility | Contents |
|---|---|---|
| `github.com/archisgore/WineTone` | Private | Full source |
| `ghcr.io/archisgore/winetone` | Private | Prebuilt images, tags `:latest`, `:stage`, `:main-<sha>`, `:stage-<sha>` |
| `huggingface.co/spaces/archisgore/winetone` | Public | Currently has source-cloning Dockerfile — broken for new builds |
| `huggingface.co/spaces/archisgore/winetone-staging` | Public | Same broken state |

---

## Step 1 — Create a GitHub PAT with `read:packages` scope

This is the only step that needs a human. The PAT lets HF Spaces
pull the private image.

1. Open <https://github.com/settings/personal-access-tokens/new>
   (the fine-grained PAT page; classic tokens work too — easier).
2. **Classic token** (simplest): <https://github.com/settings/tokens/new>
   - Note: `winetone-hf-spaces-ghcr-pull`
   - Expiration: 90 days (or longer; renew via this runbook)
   - Scopes: tick **`read:packages`** only. Nothing else.
   - Click **Generate token**.
3. Copy the token (`ghp_…`) once — it won't be shown again. Save it
   somewhere temporary; we'll plug it into both Space settings below
   and then it stops being needed.

---

## Step 2 — Set HF Space secrets (both Spaces)

The Spaces need to know how to authenticate to GHCR. HF supports
this via Space "secrets" that the Docker build/run honors.

For **each Space** (prod and staging):

1. Open the Space's Settings page:
   - <https://huggingface.co/spaces/archisgore/winetone/settings>
   - <https://huggingface.co/spaces/archisgore/winetone-staging/settings>

2. Scroll to **Variables and secrets** → **New secret**.

3. Add three secrets:

   | Name | Value |
   |---|---|
   | `DOCKER_REGISTRY_URL` | `ghcr.io` |
   | `DOCKER_REGISTRY_USERNAME` | `archisgore` |
   | `DOCKER_REGISTRY_PASSWORD` | (paste the PAT from Step 1) |

   The exact secret names HF reads for Docker auth may differ — if
   it doesn't work, check <https://huggingface.co/docs/hub/spaces-config-reference>
   for "private base image" guidance.

---

## Step 3 — Replace each Space's Dockerfile

Replace the contents of the Space's Dockerfile with a thin shim.

**Prod Space (`archisgore/winetone`) Dockerfile:**

```dockerfile
FROM ghcr.io/archisgore/winetone:latest
EXPOSE 7860
# CMD and EXPOSE are inherited from the upstream image; we re-declare
# EXPOSE for clarity in HF's UI which reads it.
```

**Staging Space (`archisgore/winetone-staging`) Dockerfile:**

```dockerfile
FROM ghcr.io/archisgore/winetone:stage
EXPOSE 7860
```

You can either:
- Edit via the HF UI (Space → "Files" tab → click `Dockerfile` → edit)
- Or clone the Space repo with `git clone https://huggingface.co/spaces/archisgore/winetone-staging`,
  overwrite `Dockerfile`, `git push`.

The Space's README.md frontmatter must declare `sdk: docker` (already
the case today). Don't change anything else in README.md — that's the
viral landing card on the Space's HF page.

---

## Step 4 — Test the staging cutover

Always staging first. Once Dockerfile is updated:

1. Trigger a factory_reboot of staging:
   ```bash
   python -c "from huggingface_hub import HfApi; HfApi().restart_space('archisgore/winetone-staging', factory_reboot=True)"
   ```
2. Watch the Space's "Logs" tab. You should see HF pull
   `ghcr.io/archisgore/winetone:stage`, then "Container running".
3. Check `https://staging.tone.wine` — the site should be up with
   the latest stage code.
4. **Sanity check that source is gone**: open
   `https://huggingface.co/spaces/archisgore/winetone-staging/tree/main`
   in an incognito tab. You should see only `Dockerfile`,
   `README.md`, `.gitattributes`. No `src/`, no `www/`.

If anything fails:
- HF logs will say either "image pull failed" (registry auth wrong)
  or "container started but errored" (image itself broken).
- The OLD container will NOT continue running after factory_reboot,
  so a failed cutover means the Space is down until reverted.
- Revert by restoring the prior Dockerfile contents from this repo's
  `git log` on the Space remote, then factory_reboot again.

---

## Step 5 — Cut prod

Repeat Step 3 + Step 4 for `archisgore/winetone` (using `:latest`
tag instead of `:stage`).

After prod cutover:
- `https://huggingface.co/spaces/archisgore/winetone/tree/main` should
  show only the 3-file shim.
- `https://tone.wine` should be live with the latest main branch.

---

## After cutover — ongoing operations

| Task | How |
|---|---|
| Deploy stage → prod | `git push origin stage:main` triggers `.github/workflows/build-image.yml`, which pushes `:latest`. Then `factory_reboot` the prod Space — HF re-pulls latest image. |
| Pin prod to a specific commit | Edit prod Space's Dockerfile to `FROM ghcr.io/archisgore/winetone:main-<sha>` and factory_reboot. |
| Rotate the GHCR PAT | Generate a new PAT (Step 1), update `DOCKER_REGISTRY_PASSWORD` on both Spaces (Step 2). No image rebuild needed. |
| Add new data release | Bump the `WINETONE_RELEASE` constant if hard-coded; otherwise the workflow picks the latest release with a `winetone-data-*.tar.gz` asset automatically. Push a commit to retrigger the build. |

---

## What the cutover does NOT do

- Doesn't touch Neon, Clerk, Anthropic, or any other backing service.
- Doesn't change the public site's behavior at all from a user's POV.
- Doesn't affect the e2e test suite — same URLs, same auth.
- Doesn't touch domain DNS, Cloudflare, or HF custom-domain mapping.

This is a deployment refactor — pure plumbing.
