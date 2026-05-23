# Runbook: HF Inference Providers — token scope, provider routing, and the Cerebras WAF gotcha

*Born from the 2026-05-23 outage: `/ask` was silently falling back
for 24+ hours. The token scope was right, the model name was right
— but HF's router was sending Llama-3.1-8B traffic to Cerebras,
whose Cloudflare WAF was 403-ing our requests. Took a real
debug session to find. This runbook is the residue.*

---

## Symptom: `/ask` returns wines but no LLM narration

The page renders, results show up, the user sees a search result —
but `result.routing.interpretation` is empty (or, in older code,
shows the explicit "router is unavailable" note). Sentry collects
an HF-related exception with `mechanism = huggingface_hub`.

That combination = the LLM call inside `winetone/llm.py` raised an
exception that the try/except caught, and the fallback recommend
ran instead. Users still see wines; they don't see LLM-translated
intent or natural-language narration.

---

## Diagnostic: does the HF_TOKEN have Inference Providers scope?

Run this from your laptop:

```bash
TOKEN='hf_xxxx'   # the same token in the Space's HF_TOKEN secret
curl -sS -w "\nHTTP %{http_code}\n" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "meta-llama/Meta-Llama-3-8B-Instruct",
    "messages": [{"role":"user","content":"reply with one word: ok"}],
    "max_tokens": 5
  }' \
  https://router.huggingface.co/v1/chat/completions
```

Three outcomes:

- **HTTP 200** with `{"choices":[{"message":{"content":"ok"}}]}` → token's scope
  and model routing both work. Problem is elsewhere (most likely:
  the Spaces still have the *old* token in their env var — see "Updating
  the Space secret" below).
- **HTTP 401 "Invalid username or password"** → token doesn't have
  Inference Providers scope. At https://huggingface.co/settings/tokens
  → click the token → **Permissions** → confirm **"Make calls to Inference
  Providers"** is enabled. On fine-grained tokens this is a checkbox
  under the "Inference" section; on classic tokens "read" is sufficient
  but the account must have the provider linked.
- **HTTP 402 / "credits exceeded"** → HF Pro Inference monthly credit
  pool exhausted. Either wait for the next billing cycle or upgrade
  the plan.
- **HTTP 403** with a Cerebras / Cloudflare HTML body → token works,
  but HF routed your call to a provider whose WAF blocks your IP.
  See "The Cerebras WAF gotcha" below.

---

## The Cerebras WAF gotcha

HF Inference Providers is a router — your HF_TOKEN goes in, the
router proxies your request to one of several commercial providers
(Cerebras, Together, Fireworks, etc.) based on what model you asked
for. Each provider has its own auth, IP-allow-list, and WAF.

We observed (2026-05-23) that:

- `meta-llama/Llama-3.1-8B-Instruct` → routed to Cerebras → Cerebras's
  Cloudflare returns 403 to our calls from both residential and HF
  Spaces IPs.
- `meta-llama/Llama-3.3-70B-Instruct` → same routing, same 403.
- `meta-llama/Meta-Llama-3-8B-Instruct` → routed elsewhere → 200 ✓
- `meta-llama/Llama-3.1-70B-Instruct` → routed elsewhere → 200 ✓
- `deepseek-ai/DeepSeek-V3` → 200 ✓
- `Qwen/Qwen2.5-72B-Instruct` → 200 ✓

The fix is one line in `src/winetone/llm.py`:

```python
DEFAULT_MODEL = os.environ.get(
    "WINETONE_LLM_MODEL", "meta-llama/Meta-Llama-3-8B-Instruct"
)
```

If a future HF routing change moves Meta-Llama-3-8B onto Cerebras
too, switch to `meta-llama/Llama-3.1-70B-Instruct` or `deepseek-ai/DeepSeek-V3`.
The `WINETONE_LLM_MODEL` env var lets you override without code.

To find which model goes to which provider quickly:

```bash
TOKEN='hf_xxxx'
for model in \
  meta-llama/Meta-Llama-3-8B-Instruct \
  meta-llama/Llama-3.1-70B-Instruct \
  deepseek-ai/DeepSeek-V3 \
  Qwen/Qwen2.5-72B-Instruct; do
  echo "--- $model ---"
  curl -sS -o /dev/null -w "HTTP %{http_code}\n" \
    -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$model\",\"messages\":[{\"role\":\"user\",\"content\":\"ok\"}],\"max_tokens\":3}" \
    https://router.huggingface.co/v1/chat/completions
done
```

Any model that returns 200 is fair game.

---

## Updating the Space secret

If the diagnostic shows the token works but the live Space still
falls back, the Space probably still has the **old** value of
`HF_TOKEN` in its env. HF Spaces don't always pick up secret
changes on a normal restart; a `factory_reboot=True` is needed.

```python
from huggingface_hub import HfApi
api = HfApi()
# Need a token with WRITE scope (the read-only inference token can't
# update secrets; use a separate deploy/write token).
deploy_token = "hf_xxxx_with_write"
new_value    = "hf_xxxx_with_inference_scope"
for sp in ("archisgore/winetone", "archisgore/winetone-staging"):
    api.add_space_secret(sp, "HF_TOKEN", new_value,
                         token=deploy_token,
                         description="HF Inference Providers token")
    api.restart_space(sp, factory_reboot=True, token=deploy_token)
```

`factory_reboot=True` matters: a regular restart can keep the
existing container's environment variables.

---

## What graceful failure looks like

The fallback path in `llm.py` returns a recommend on the raw query
text whenever the LLM call raises (any exception, not just 401).
This keeps `/ask` functioning end-to-end even when:

- The token is broken
- A provider's WAF blocks us
- HF's router itself is down
- The credit pool is exhausted

In the UI we deliberately don't tell the user about the failure —
they see "Results for: <their query>" with the wines a direct
search produces. Sentry still gets the underlying exception via
`huggingface_hub`'s SDK integration, so ops know.

If you want to see the failure during debugging, the response
shape includes `result.routing.fallback = True` — toggling that
in the template would expose the failure mode again.

---

## Cost reference

| Model | Tier | Approximate $/query |
|---|---|---|
| `meta-llama/Meta-Llama-3-8B-Instruct` | small | ~$0.0001 |
| `meta-llama/Llama-3.1-70B-Instruct` | medium | ~$0.0005 |
| `deepseek-ai/DeepSeek-V3` | large | ~$0.001 |
| `Qwen/Qwen2.5-72B-Instruct` | medium | ~$0.0005 |

For routing-prompt usage (short input, ~50-token JSON output), all
four are well under one cent per call. Routing-prompt throughput at
WineTone's current scale is in the dozens-per-day range; budget
impact is negligible.

---

## When this runbook is *not* the right one

- **The LLM returns 200 but gives nonsense JSON.** That's a model
  capability/prompt-quality issue, not an Inference Providers
  issue. Look at the `SYSTEM_PROMPT` in `llm.py`.
- **The scanner (`/wines/scan`) is broken.** That's the Anthropic
  API (Claude Vision), not HF Inference. Check `ANTHROPIC_API_KEY`
  in the Space secrets.
- **The encoder won't load.** That's sentence-transformers + the
  HF model hub directly (`archisgore/bge-small-winetone`), not the
  router. Check `HF_HOME` cache and the model's HF Hub repo
  reachability.
