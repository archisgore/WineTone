"""Fine-tune bge-small-en-v1.5 on the WineTone corpus.

The pre-trained encoder was trained on general web text. Wine-specific
terms ("petrol," "graphite," "stewed fruit," "VA") get embedded by
their *general English* meanings, which is roughly never what a wine
reviewer means. This script contrastively fine-tunes bge-small so that
wine-domain vocabulary tightens up.

Approach: MultipleNegativesRankingLoss with positive pairs drawn from
the source-records table. For each canonical wine that has ≥ 2
reviews, every pair of reviews is a positive pair — they describe the
*same wine* and should land close in embedding space. The other
in-batch examples (other wines) serve as implicit negatives. No
explicit hard-negative mining yet; that's a v2 improvement.

Requirements
------------
This script runs OUTSIDE the deployed Space. It needs:

  - local CedarDB populated by `winetone build canonical` (has the
    source_records table that the deployed Neon copy doesn't keep).
  - sentence-transformers + torch installed (`pip install
    sentence-transformers`). Not in the regular WineTone deps — they
    pull in transformers which is ~1GB.
  - a GPU. CUDA is fastest; Apple-Silicon MPS works but slower.
    On CPU expect ~10× the runtime — feasible but no fun.

What it produces
----------------
A fine-tuned SentenceTransformer model at
`data/models/bge-small-winetone/`. To use it in the rest of the
pipeline, export to ONNX and either:

  (a) point fastembed at the local ONNX file (requires a small
      fastembed config — see fastembed docs on custom models), OR
  (b) swap the encoder layer to sentence-transformers in embed.py
      (heavier deploy image but simpler integration).

Then re-encode the full corpus with `winetone build embeddings`
(the new model name lands in wine_embeddings.embedding_model so
downstream calibration projects from a consistent space), package a
new release tarball, and trigger a Space rebuild.

Usage
-----
  pip install sentence-transformers
  python scripts/fine_tune_encoder.py \\
      --epochs 1 \\
      --batch-size 32 \\
      --max-pairs 200000 \\
      --output data/models/bge-small-winetone

Add `--dry-run` to skip the actual training and just inspect the data.
"""

from __future__ import annotations

import argparse
import itertools
import logging
import random
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

log = logging.getLogger("fine_tune_encoder")


# --- data --------------------------------------------------------------


def _load_review_pairs(
    max_pairs: int,
    min_reviews_per_wine: int = 2,
    seed: int = 42,
) -> list[tuple[str, str]]:
    """Pull positive (review_a, review_b) pairs from source_records.

    For each canonical wine with ≥ min_reviews_per_wine, sample
    pair-of-distinct-reviews. Caps total pairs at `max_pairs`.
    """
    from winetone import db

    log.info("loading source_records from local CedarDB ...")
    df = pd.read_sql(
        text("""
            SELECT wine_id, description AS review_text
            FROM source_records
            WHERE description IS NOT NULL AND length(description) > 40
        """),
        db.engine(),
    )
    log.info("  loaded %d source rows", len(df))

    grouped = df.groupby("wine_id")["review_text"].agg(list)
    grouped = grouped[grouped.map(len) >= min_reviews_per_wine]
    log.info(
        "  %d wines have ≥%d reviews (median %d, max %d)",
        len(grouped), min_reviews_per_wine,
        int(grouped.map(len).median()) if len(grouped) else 0,
        int(grouped.map(len).max()) if len(grouped) else 0,
    )

    rng = random.Random(seed)
    pairs: list[tuple[str, str]] = []
    for reviews in grouped:
        # For each wine, sample at most ~10 pairs to keep balance —
        # otherwise wines with 30 reviews would dominate.
        if len(reviews) > 6:
            sub = rng.sample(reviews, 6)
        else:
            sub = reviews
        for a, b in itertools.combinations(sub, 2):
            pairs.append((a, b))
            if len(pairs) >= max_pairs:
                break
        if len(pairs) >= max_pairs:
            break

    rng.shuffle(pairs)
    log.info("  built %d positive pairs", len(pairs))
    return pairs


# --- training ----------------------------------------------------------


def _detect_device() -> str:
    """Return the best available torch device for fine-tuning."""
    try:
        import torch
    except ImportError:
        log.error("torch not installed — run `pip install sentence-transformers`")
        sys.exit(2)
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def fine_tune(
    pairs: list[tuple[str, str]],
    output_dir: Path,
    epochs: int,
    batch_size: int,
    base_model: str,
) -> None:
    """Run the contrastive fine-tune."""
    try:
        from sentence_transformers import (
            InputExample, SentenceTransformer, losses,
        )
        from torch.utils.data import DataLoader
    except ImportError:
        log.error(
            "sentence-transformers not installed. "
            "Run: pip install sentence-transformers"
        )
        sys.exit(2)

    device = _detect_device()
    log.info("device: %s", device)
    log.info("loading base model: %s", base_model)
    model = SentenceTransformer(base_model, device=device)

    examples = [InputExample(texts=[a, b]) for a, b in pairs]
    loader = DataLoader(examples, shuffle=True, batch_size=batch_size)
    loss = losses.MultipleNegativesRankingLoss(model)

    output_dir.mkdir(parents=True, exist_ok=True)
    log.info(
        "fine-tuning · pairs=%d · batch_size=%d · epochs=%d · output=%s",
        len(pairs), batch_size, epochs, output_dir,
    )

    # warmup_steps: ~10% of one epoch is fine for small fine-tunes.
    steps_per_epoch = max(1, len(loader))
    warmup = max(50, steps_per_epoch // 10)

    model.fit(
        train_objectives=[(loader, loss)],
        epochs=epochs,
        warmup_steps=warmup,
        output_path=str(output_dir),
        show_progress_bar=True,
        use_amp=(device == "cuda"),
    )
    log.info("training complete · model saved to %s", output_dir)


def export_onnx(model_dir: Path) -> None:
    """Export the fine-tuned model to ONNX so fastembed can load it."""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        log.error("sentence-transformers not installed")
        return
    try:
        import torch
    except ImportError:
        return

    onnx_path = model_dir / "model.onnx"
    log.info("exporting ONNX → %s", onnx_path)
    model = SentenceTransformer(str(model_dir))
    model.eval()
    tokenizer = model.tokenizer
    dummy = tokenizer(
        ["wine sample"], padding=True, truncation=True, return_tensors="pt",
    )
    # SentenceTransformer's first module is the Transformer wrapper.
    backbone = model[0].auto_model
    torch.onnx.export(
        backbone,
        (dummy["input_ids"], dummy["attention_mask"]),
        str(onnx_path),
        input_names=["input_ids", "attention_mask"],
        output_names=["last_hidden_state"],
        dynamic_axes={
            "input_ids": {0: "batch", 1: "seq"},
            "attention_mask": {0: "batch", 1: "seq"},
            "last_hidden_state": {0: "batch", 1: "seq"},
        },
        opset_version=14,
    )
    log.info("ONNX export done · %d bytes", onnx_path.stat().st_size)


# --- main --------------------------------------------------------------


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-model", default="BAAI/bge-small-en-v1.5")
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--max-pairs", type=int, default=200_000)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/models/bge-small-winetone"),
    )
    parser.add_argument(
        "--export-onnx", action="store_true",
        help="After training, also export to ONNX for fastembed loading.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Just count + sample pairs; skip the actual training.",
    )
    args = parser.parse_args()

    pairs = _load_review_pairs(max_pairs=args.max_pairs)
    if not pairs:
        log.error("no training pairs found — is local CedarDB populated "
                  "with `winetone build canonical`?")
        sys.exit(1)

    log.info("sample pair (first):")
    log.info("  A: %s", pairs[0][0][:160])
    log.info("  B: %s", pairs[0][1][:160])

    if args.dry_run:
        log.info("--dry-run set; not training. exit.")
        return

    fine_tune(
        pairs,
        output_dir=args.output,
        epochs=args.epochs,
        batch_size=args.batch_size,
        base_model=args.base_model,
    )

    if args.export_onnx:
        export_onnx(args.output)


if __name__ == "__main__":
    main()
