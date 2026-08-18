"""Local speech-to-text via faster-whisper (free, offline after model download).

Push-to-talk audio is normalized with ffmpeg to 16 kHz mono WAV, then transcribed
on-device. No cloud API key required.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import tempfile
import threading
from pathlib import Path

from .config import store

log = logging.getLogger("jarvis.stt")

MAX_BYTES = 25 * 1024 * 1024
DEFAULT_MODEL = "base.en"
DEFAULT_DEVICE = "cpu"
DEFAULT_COMPUTE = "int8"

_model = None
_model_key: tuple[str, str, str] | None = None
_model_lock = threading.Lock()


def _settings() -> tuple[str, str, str]:
    cfg = store.get()
    stt = getattr(cfg, "stt", None)
    model = (getattr(stt, "model", None) or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    # Migrate old cloud whisper model names to a local faster-whisper size.
    if model in {"whisper-1", "whisper", "gpt-4o-transcribe"}:
        model = DEFAULT_MODEL
    device = (getattr(stt, "device", None) or DEFAULT_DEVICE).strip() or DEFAULT_DEVICE
    compute = (getattr(stt, "compute_type", None) or DEFAULT_COMPUTE).strip() or DEFAULT_COMPUTE
    return model, device, compute


def _load_model(model_size: str, device: str, compute_type: str):
    """Load (or reuse) a WhisperModel. Blocking — call from a worker thread."""
    global _model, _model_key
    key = (model_size, device, compute_type)
    with _model_lock:
        if _model is not None and _model_key == key:
            return _model
        from faster_whisper import WhisperModel

        log.info(
            "Loading faster-whisper model=%s device=%s compute=%s",
            model_size,
            device,
            compute_type,
        )
        _model = WhisperModel(model_size, device=device, compute_type=compute_type)
        _model_key = key
        return _model


def _suffix_for(filename: str, mime: str) -> str:
    name = (filename or "").lower()
    for ext in (".webm", ".ogg", ".mp4", ".m4a", ".wav", ".mp3", ".mpeg", ".aac"):
        if name.endswith(ext):
            return ext
    mime = (mime or "").lower()
    if "ogg" in mime:
        return ".ogg"
    if "mp4" in mime or "m4a" in mime or "aac" in mime:
        return ".mp4"
    if "wav" in mime:
        return ".wav"
    if "mpeg" in mime or "mp3" in mime:
        return ".mp3"
    return ".webm"


def _ffmpeg_to_wav(src: Path, dst: Path) -> None:
    """Decode any browser recording into 16 kHz mono PCM WAV for Whisper."""
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(dst),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not dst.is_file() or dst.stat().st_size < 44:
        detail = (proc.stderr or proc.stdout or "ffmpeg failed").strip()
        raise RuntimeError(f"Could not decode audio ({detail})")


def _transcribe_wav(wav_path: str, model_size: str, device: str, compute_type: str) -> str:
    model = _load_model(model_size, device, compute_type)
    # VAD often drops short push-to-talk clips; keep it off for reliability.
    segments, info = model.transcribe(
        wav_path,
        language="en",
        vad_filter=False,
        beam_size=1,
        best_of=1,
        temperature=0.0,
        condition_on_previous_text=False,
    )
    parts: list[str] = []
    for seg in segments:
        text = (seg.text or "").strip()
        if text:
            parts.append(text)
    text = " ".join(parts).strip()
    duration = getattr(info, "duration", None)
    log.info(
        "STT finished model=%s duration=%s text_len=%d",
        model_size,
        duration,
        len(text),
    )
    return text


async def transcribe(audio: bytes, filename: str = "audio.webm", mime: str = "audio/webm") -> str:
    """Return transcript text. Empty string if no speech (not an error)."""
    if not audio:
        raise ValueError("Empty audio")
    if len(audio) > MAX_BYTES:
        raise ValueError("Audio too large (max 25MB)")

    model_size, device, compute_type = _settings()
    suffix = _suffix_for(filename, mime)

    src_path: str | None = None
    wav_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(audio)
            src_path = tmp.name
        wav_tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        wav_path = wav_tmp.name
        wav_tmp.close()

        await asyncio.to_thread(_ffmpeg_to_wav, Path(src_path), Path(wav_path))
        text = await asyncio.to_thread(
            _transcribe_wav, wav_path, model_size, device, compute_type
        )
        return text
    finally:
        for path in (src_path, wav_path):
            if not path:
                continue
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass
