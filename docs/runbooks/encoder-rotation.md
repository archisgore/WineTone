# Runbook: Rotating the Wine Embedding Encoder

*Last performed 2026-05-21 — fine-tuned bge-small-en-v1.5 →
archisgore/bge-small-winetone. ~3 hours wall-clock end-to-end.*

This is the playbook for swapping the wine-embedding encoder when
you have a new fine-tuned model you want in production. Most of the
steps are batch jobs that can run in the background while you work on
something else.

The whole flow is **off-line** in the sense that the live site keeps
serving with the OLD encoder until the very last step. There's no
window where the corpus is half-old, half-new; we promote
atomically.

---

## Prerequisites

- Local CedarDB up (`docker start winetone-cedardb`) and populated
  with `winetone build canonical` — fine-tuning reads `source_records`
  which only lives locally, not on Neon.
- HF account with write token cached (`huggingface-cli login` or
  `~/.cache/huggingface/token`).
- Neon access via `WINETONE_DB_URL` (the paid tier — re-encoding
  blows through the free 512 MB cap).
- `pip install -e ".[finetune]"` to get sentence-transformers,
  accelerate, datasets.

---

## The five steps

### 1. Train the new encoder

Edit `scripts/fine_tune_encoder.py` if you're changing pairs sampling
or hyperparameters; otherwise the defaults are sensible.

```bash
nohup .venv/bin/python scripts/fine_tune_encoder.py \
    --epochs 1 \
    --batch-size 32 \
    --max-pairs 200000 \
    --output data/models/<new-model-dir> \
    > /tmp/finetune.log 2>&1 &
```

On Apple Silicon (MPS) this takes ~30-40 min for 200K pairs × 1 epoch.
On a CUDA T4/A100 it's under 5 min. Monitor with:

```bash
LC_ALL=C tail -c 6000 /tmp/finetune.log | LC_ALL=C tr '\r' '\n' | tail -10
```

Output lives at `data/models/<new-model-dir>/` — safetensors weights
+ tokenizer + sentence_transformers config. Validate it loads:

```python
from sentence_transformers import SentenceTransformer
m = SentenceTransformer("data/models/<new-model-dir>")
v = m.encode("bold tannic Cabernet", normalize_embeddings=True)
assert v.shape == (384,)  # or whatever your dim is
```

### 2. Push the model to HF Hub

```python
from huggingface_hub import HfApi
api = HfApi()
REPO = "archisgore/<new-model-name>"
api.create_repo(REPO, repo_type="model", exist_ok=True, private=False)
api.upload_folder(
    folder_path="data/models/<new-model-dir>",
    repo_id=REPO,
    repo_type="model",
    commit_message="WineTone fine-tune YYYY-MM-DD",
)
```

Verify via `api.model_info(REPO).siblings` — expect `model.safetensors`,
the four config JSONs, the two tokenizer JSONs.

### 3. Re-encode the full corpus

```bash
export WINETONE_DB_URL='postgresql+psycopg://...neon.tech/neondb?sslmode=require'
nohup .venv/bin/python scripts/reencode_corpus.py \
    --model data/models/<new-model-dir> \
    --batch-size 64 \
    --model-name archisgore/<new-model-name> \
    > /tmp/reencode.log 2>&1 &
```

On MPS: ~7 min encoding (164K wines @ ~6 batches/sec @ batch 64) +
~8 min uploading to Neon (~410 rows/sec).

`scripts/reencode_corpus.py` is resumable. It uses
`ON CONFLICT (wine_id) DO UPDATE` so a partial run leaves a consistent
state and a re-run converges. TCP keepalives prevent the SSL idle
timeout that killed the first attempt at the original deploy.

Confirm completion:

```sql
SELECT embedding_model, COUNT(*) FROM wine_embeddings
GROUP BY embedding_model;
```

Should show **only** the new model name with 164,069 rows (or
whatever current count is — `SELECT COUNT(*) FROM wines` to check).

### 4. Update the code + push

```python
# src/winetone/embed.py
MODEL_NAME = os.environ.get("WINETONE_ENCODER", "archisgore/<new-model-name>")
```

The env-var fallback lets you A/B against the previous model without
a code change (just set `WINETONE_ENCODER=archisgore/<old-model-name>`
as a Space variable to test).

```bash
git add src/winetone/embed.py
git commit -m "feat(encoder): rotate to <new-model-name>"
git push origin main
```

### 5. Factory-reboot the Space

```python
from huggingface_hub import HfApi
HfApi().restart_space("archisgore/winetone", factory_reboot=True)
```

Build is ~3 min. During the build, the OLD container keeps serving
with the OLD encoder against the NEW Neon embeddings. **This is
fine** for ~3 min: the embeddings are still in the same 384-dim
space and the OLD encoder's queries still produce sensible cosines
against the NEW corpus vectors (just worse than the NEW encoder
would). When the NEW container takes over, you have full alignment
again.

Then verify with a smoke query:

```bash
curl -X POST --data-urlencode "query=earthy cherry forest floor mushroom" \
  https://tone.wine/ask/query
```

Should return Pinot Noir territory in the top results.

---

## What you do NOT need to redo

- **`wines.tsv` (FTS column).** The sparse channel is over text, not
  embeddings. The tsv stays valid regardless of which encoder
  produced the dense vectors.
- **User projections** (`user_projections` table). These are linear
  maps `A·L + b` in the same 384-dim space; a new encoder produces
  vectors in the same shape, so the projections still apply
  mathematically. They'll be *suboptimal* until users re-fit (their
  old training pairs were against old W coordinates), but they
  won't crash.

The right time to **invite users to refit** is in a release-notes
blog post — make the lift visible.

## Rollback

If the new encoder is worse and you want to revert:

```bash
# Edit src/winetone/embed.py back to the previous MODEL_NAME
git revert <the-rotation-commit>
git push origin main

# Re-encode the corpus with the old model
.venv/bin/python scripts/reencode_corpus.py \
    --model <previous-local-path-OR-pull-from-HF> \
    --model-name archisgore/<previous-model-name>

# Factory-reboot
.venv/bin/python -c "
from huggingface_hub import HfApi
HfApi().restart_space('archisgore/winetone', factory_reboot=True)
"
```

Total rollback time: 1-2 hours (mostly re-encoding) + ~3 min rebuild.
Mitigation: keep the previous model's weights on HF Hub forever —
they're 133 MB, costs nothing, and gives you a no-retraining-needed
rollback path.

## What's stored where

| Artifact | Storage | Versioning |
|---|---|---|
| Encoder weights | HF Hub model repo `archisgore/<name>` | git via HF Hub commits |
| Wine embeddings | Neon `wine_embeddings` table | `embedding_model` column tags which encoder produced them |
| User projections | Neon `user_projections` + `user_calibration_history` | history table is append-only |
| Source review pairs | Local CedarDB `source_records` | not in releases — too large + privacy-sensitive |
| Release tarballs | GitHub releases on `archisgore/WineTone` | tagged `v2026.MM.DD` |
