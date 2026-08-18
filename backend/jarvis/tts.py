"""Server-side text-to-speech for JARVIS.

Uses Microsoft Edge neural voices (via edge-tts) so playback works in any
browser or the Tauri desktop shell — independent of Web Speech API / OS TTS.

Default voice is a refined British male, pitched and paced for a butler-robot
delivery (polite, measured, slightly formal).
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
from pathlib import Path

import edge_tts

from .config import DATA_DIR, store

log = logging.getLogger("jarvis.tts")

CACHE_DIR = DATA_DIR / "tts_cache"
MAX_CHARS = 2000

# Refined British male — closest stock neural match to a Stark-style butler.
DEFAULT_VOICE = "en-GB-ThomasNeural"
DEFAULT_RATE = "-12%"
DEFAULT_PITCH = "-6Hz"

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _clean(text: str) -> str:
    text = _CTRL.sub(" ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > MAX_CHARS:
        text = text[: MAX_CHARS - 1].rstrip() + "…"
    return text


def _cache_key(text: str, voice: str, rate: str, pitch: str) -> str:
    raw = f"{voice}|{rate}|{pitch}|{text}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


async def synthesize(text: str) -> bytes:
    """Return MP3 bytes for `text` using the configured JARVIS voice."""
    cleaned = _clean(text)
    if not cleaned:
        raise ValueError("Nothing to speak")

    cfg = store.get()
    tts = getattr(cfg, "tts", None)
    voice = (getattr(tts, "voice", None) or DEFAULT_VOICE).strip() or DEFAULT_VOICE
    rate = (getattr(tts, "rate", None) or DEFAULT_RATE).strip() or DEFAULT_RATE
    pitch = (getattr(tts, "pitch", None) or DEFAULT_PITCH).strip() or DEFAULT_PITCH

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{_cache_key(cleaned, voice, rate, pitch)}.mp3"
    if path.is_file() and path.stat().st_size > 0:
        return path.read_bytes()

    communicate = edge_tts.Communicate(cleaned, voice, rate=rate, pitch=pitch)
    buf = io.BytesIO()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buf.write(chunk["data"])
    audio = buf.getvalue()
    if not audio:
        raise RuntimeError("TTS produced no audio")

    try:
        path.write_bytes(audio)
    except OSError as exc:
        log.warning("TTS cache write failed: %s", exc)

    return audio
