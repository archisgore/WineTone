"""Wine-label scanner: Claude Vision → structured fields → corpus match.

User uploads a photo of a wine label. We send the image to Claude
(Haiku tier — cheap, fast, accurate enough for OCR-like label
extraction) with a structured prompt asking for the producer, wine
name, vintage, country, and variety as JSON. The model also reports
its own confidence so we can route between "match against the
existing 164K-wine corpus" and "send the user to /wines/new
pre-filled."

Design notes:

- We **don't** store the uploaded image. It's only kept in memory
  long enough to send to Anthropic. The privacy policy promises
  nothing about images (they're not enumerated as collected data),
  and silently retaining them would surprise users.
- We use ``claude-haiku-4-5`` because labels are short and the
  task is OCR-like. Opus would cost 5-10x for marginal accuracy
  improvement on stylized wine fonts.
- The model is asked to return strict JSON. If parsing fails we
  surface the raw text to logs and route to /wines/new with
  empty fields rather than crashing.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re

log = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5"
MAX_IMAGE_BYTES = 6 * 1024 * 1024  # 6 MB safety cap; Anthropic accepts up to ~30 MB

SYSTEM_PROMPT = (
    "You extract wine identification from a photo of a wine label. "
    "Return STRICT JSON only — no markdown, no preamble. "
    "Schema: {\"producer\": str|null, \"wine_name\": str|null, "
    "\"vintage\": int|null, \"country\": str|null, \"variety\": str|null, "
    "\"confidence\": \"high\"|\"medium\"|\"low\", "
    "\"notes\": str (one short sentence describing what you saw)}. "
    "Use null for any field not visible. Vintage must be a 4-digit year, "
    "or null if no year is on the label. Producer is the winery (e.g. "
    "'Domaine de la Romanée-Conti', 'Penfolds', 'Château Margaux'). "
    "wine_name is the specific cuvée or appellation (e.g. 'Grange', "
    "'Brunello di Montalcino', 'Bin 389'). "
    "Variety is the grape (e.g. 'Cabernet Sauvignon', 'Pinot Noir'). "
    "Country uses the English name. "
    "If the image is not a wine label, return confidence='low' and "
    "all fields null with notes explaining what you see."
)


def _detect_media_type(image_bytes: bytes) -> str:
    """Sniff the image type from its magic bytes."""
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"GIF87a") or image_bytes.startswith(b"GIF89a"):
        return "image/gif"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"  # most camera uploads


def _maybe_downsize(image_bytes: bytes) -> bytes:
    """If the upload is bigger than MAX_IMAGE_BYTES, downsize via Pillow
    so we don't fight Anthropic's API limit or burn tokens on a 10MP photo
    when a 1024px-wide image gives identical OCR accuracy.
    """
    if len(image_bytes) <= MAX_IMAGE_BYTES:
        return image_bytes
    try:
        from io import BytesIO

        from PIL import Image
        img = Image.open(BytesIO(image_bytes))
        # Drop alpha so JPEG encoding works.
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")
        # Target 1600px on the long side — plenty for label reading.
        w, h = img.size
        if max(w, h) > 1600:
            scale = 1600 / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        out = BytesIO()
        img.save(out, format="JPEG", quality=85, optimize=True)
        result = out.getvalue()
        log.info("scanner: downsized image %d → %d bytes",
                 len(image_bytes), len(result))
        return result
    except Exception as e:  # noqa: BLE001
        log.warning("scanner: downsizing failed (%s); sending original", e)
        return image_bytes


def extract_label(image_bytes: bytes) -> dict:
    """Send image to Claude Vision and parse the structured response.

    Returns a dict with the schema described in SYSTEM_PROMPT, plus
    an ``error`` key if extraction failed entirely.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return {"error": "scanner disabled — ANTHROPIC_API_KEY not configured"}

    try:
        import anthropic
    except ImportError:
        return {"error": "anthropic SDK not installed"}

    image_bytes = _maybe_downsize(image_bytes)
    media_type = _detect_media_type(image_bytes)
    b64 = base64.b64encode(image_bytes).decode("ascii")

    client = anthropic.Anthropic(api_key=api_key, timeout=60.0)
    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=400,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": media_type, "data": b64,
                    }},
                    {"type": "text", "text": "Extract the wine."},
                ],
            }],
        )
    except Exception as e:  # noqa: BLE001
        log.warning("scanner: anthropic call failed: %s", e)
        return {"error": f"vision model error: {e}"}

    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    log.info("scanner: raw response (%d chars): %s", len(text), text[:300])

    # Strip ```json fences if the model added them despite the prompt.
    text = re.sub(r"^\s*```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        log.warning("scanner: JSON parse failed: %s; raw: %r", e, text[:300])
        return {"error": "model returned non-JSON output", "raw": text}

    # Normalize fields: clamp vintage to plausible range; lowercase / title-case where helpful.
    v = parsed.get("vintage")
    if isinstance(v, str) and v.isdigit():
        v = int(v)
    if isinstance(v, int) and not (1800 <= v <= 2100):
        v = None
    parsed["vintage"] = v
    for k in ("producer", "wine_name", "country", "variety"):
        if isinstance(parsed.get(k), str):
            parsed[k] = parsed[k].strip() or None
    if parsed.get("confidence") not in ("high", "medium", "low"):
        parsed["confidence"] = "low"
    parsed["model"] = MODEL
    return parsed
