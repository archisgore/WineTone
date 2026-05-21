"""Re-encode the entire wine corpus with the fine-tuned encoder and
upload the new embeddings to Neon.

Run after `scripts/fine_tune_encoder.py` produces a SentenceTransformer
model at `data/models/bge-small-winetone/`. This script:

  1. Loads the fine-tuned model.
  2. Pulls every wine's embedding-text from LOCAL CedarDB (display
     prefix + review_text_all, same shape as embed._build_embedding_text).
  3. Encodes them in batches on whatever device is fastest (MPS / CUDA / CPU).
  4. Writes the new (wine_id, embedding, embedding_model) rows to NEON,
     replacing the existing wine_embeddings entirely.

We use sentence-transformers (not fastembed) directly here. fastembed's
custom-ONNX loading is fiddly and this is a one-shot batch job — the
speed difference doesn't matter.

Usage:
    python scripts/reencode_corpus.py --model data/models/bge-small-winetone
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

log = logging.getLogger("reencode")


def _embedding_text(row: dict) -> str:
    """Mirror embed._build_embedding_text — keep these in sync."""
    parts: list[str] = []
    if row.get("variety"):
        parts.append(f"variety: {row['variety']}.")
    if row.get("region"):
        parts.append(f"region: {row['region']}.")
    if row.get("country"):
        parts.append(f"country: {row['country']}.")
    v = row.get("vintage")
    if v is not None and pd.notna(v):
        parts.append(f"vintage: {int(v)}.")
    rta = row.get("review_text_all") or ""
    if rta:
        parts.append(rta)
    return " ".join(parts)[:2000]


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument(
        "--local-db",
        default="postgresql+psycopg://winetone:winetone@localhost:5432/winetone",
        help="Source: where wines+text live (local CedarDB).",
    )
    parser.add_argument(
        "--remote-db",
        default=os.environ.get("WINETONE_DB_URL", ""),
        help="Target: where to write new embeddings. "
             "Defaults to $WINETONE_DB_URL (must be set, points at Neon).",
    )
    parser.add_argument(
        "--model-name",
        default="archisgore/bge-small-winetone",
        help="The string that ends up in wine_embeddings.embedding_model. "
             "Use the eventual HF repo name; the file path local to this "
             "run is separate via --model.",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Stop after this many wines (0 = entire corpus). Debug aid.",
    )
    args = parser.parse_args()

    if not args.remote_db:
        log.error("--remote-db or $WINETONE_DB_URL required")
        sys.exit(2)

    # Load the fine-tuned model.
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        log.error("sentence-transformers not installed; "
                  "run `pip install -e .[finetune]`")
        sys.exit(2)
    import torch
    if torch.cuda.is_available():
        device = "cuda"
    elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        device = "mps"
    else:
        device = "cpu"
    log.info("loading model from %s on device=%s", args.model, device)
    model = SentenceTransformer(str(args.model), device=device)

    # Pull source rows from local CedarDB.
    log.info("reading wine text from local CedarDB ...")
    local = create_engine(args.local_db)
    sql = """
        SELECT w.wine_id, w.variety, w.region, w.country, w.vintage,
               f.review_text_all
        FROM wines w
        LEFT JOIN wine_features f ON f.wine_id = w.wine_id
        ORDER BY w.wine_id
    """
    df = pd.read_sql(text(sql), local)
    if args.limit > 0:
        df = df.head(args.limit)
    log.info("  %d wines to encode", len(df))

    # Encode in batches.
    texts = df.apply(lambda r: _embedding_text(r.to_dict()), axis=1).tolist()
    t0 = time.monotonic()
    log.info("encoding (batch_size=%d, device=%s) ...", args.batch_size, device)
    vecs = model.encode(
        texts,
        batch_size=args.batch_size,
        show_progress_bar=True,
        convert_to_numpy=True,
        normalize_embeddings=True,
    )
    dt = time.monotonic() - t0
    log.info("encoded %d wines in %.1fs (%.1f wines/sec)",
             len(vecs), dt, len(vecs) / max(dt, 1e-6))
    log.info("vector shape: %s, dtype: %s", vecs.shape, vecs.dtype)
    assert vecs.shape[1] == 384, f"unexpected dim {vecs.shape[1]}"

    # Write to Neon. We KEEP existing rows (in case a prior run partially
    # completed); ON CONFLICT DO NOTHING handles the overlap. Idempotent
    # re-runs are fine.
    #
    # Connection-level keepalive matters here — without it the SSL
    # connection silently dies during ~30+ min uploads (we saw "SSL
    # SYSCALL error: Operation timed out" on the first attempt).
    remote = create_engine(
        args.remote_db,
        pool_pre_ping=True,
        connect_args={
            "keepalives": 1, "keepalives_idle": 30,
            "keepalives_interval": 10, "keepalives_count": 5,
        },
    )
    ac = remote.execution_options(isolation_level="AUTOCOMMIT")

    log.info("ensuring wine_embeddings table exists ...")
    with ac.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS wine_embeddings (
                wine_id         TEXT PRIMARY KEY,
                embedding       vector(384) NOT NULL,
                embedding_model TEXT NOT NULL
            )
        """))

    # Smaller chunks → shorter transactions → less exposure to SSL idle
    # disconnects on a slow uplink. 500 rows × ~3KB each ≈ 1.5MB per round-trip.
    log.info("uploading new embeddings to remote ...")
    chunk = 500
    t0 = time.monotonic()
    n_inserted = 0
    for start in range(0, len(df), chunk):
        end = min(start + chunk, len(df))
        params = []
        for i in range(start, end):
            v = vecs[i]
            v_str = "[" + ",".join(f"{x:.6f}" for x in v.tolist()) + "]"
            params.append({
                "w": str(df.iloc[i]["wine_id"]),
                "e": v_str,
                "m": args.model_name,
            })
        # Retry once on transient connection errors.
        for attempt in (1, 2):
            try:
                with remote.begin() as conn:
                    conn.execute(text("""
                        INSERT INTO wine_embeddings (wine_id, embedding, embedding_model)
                        VALUES (:w, CAST(:e AS vector(384)), :m)
                        ON CONFLICT (wine_id) DO UPDATE SET
                            embedding = EXCLUDED.embedding,
                            embedding_model = EXCLUDED.embedding_model
                    """), params)
                n_inserted += len(params)
                break
            except Exception as e:
                if attempt == 1:
                    log.warning("chunk %d failed (%s); retrying...", start, e)
                    time.sleep(2)
                else:
                    log.error("chunk %d failed twice; giving up.", start)
                    raise
        if (start // chunk) % 20 == 0:
            elapsed = time.monotonic() - t0
            rate = (n_inserted / elapsed) if elapsed > 0 else 0
            log.info("  %d/%d uploaded (%.0f/s, ETA %.0fs)",
                     end, len(df), rate,
                     max(0, (len(df) - end) / max(rate, 1)))

    with remote.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM wine_embeddings")).scalar()
        sz = conn.execute(text(
            "SELECT pg_size_pretty(pg_database_size(current_database()))"
        )).scalar()
    log.info("done · %d rows in wine_embeddings · DB size %s", n, sz)


if __name__ == "__main__":
    main()
