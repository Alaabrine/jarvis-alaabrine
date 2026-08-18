"""Media attachment ingest for vision-capable prompts.

Images are normalised and exposed as data-URLs for the OpenAI-compatible vision API.
Videos have a handful of representative frames extracted via ffmpeg (when available).
"""

from __future__ import annotations

import base64
import io
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from .config import DATA_DIR

UPLOAD_DIR = DATA_DIR / "uploads"
MAX_ATTACHMENTS = 6
MAX_BYTES = 40 * 1024 * 1024  # 40 MB raw upload
MAX_IMAGE_EDGE = 1536
VIDEO_FRAME_COUNT = 4

IMAGE_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/gif",
}
VIDEO_TYPES = {
    "video/mp4",
    "video/webm",
    "video/quicktime",
    "video/x-matroska",
    "video/mpeg",
}


@dataclass
class StoredAttachment:
    id: str
    name: str
    mime: str
    kind: str  # image | video | frame
    path: str
    parent_id: str | None = None  # frame -> video id
    note: str | None = None

    def to_meta(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "mime": self.mime,
            "kind": self.kind,
            "path": self.path,
            "parent_id": self.parent_id,
            "note": self.note,
            "url": f"/api/media/{self.id}",
        }


class MediaError(ValueError):
    pass


def _ensure_upload_dir() -> Path:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return UPLOAD_DIR


def _guess_ext(mime: str, name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix:
        return suffix
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
        "video/quicktime": ".mov",
        "video/x-matroska": ".mkv",
    }
    return mapping.get(mime, ".bin")


def _normalise_image_bytes(raw: bytes) -> tuple[bytes, str]:
    """Resize/compress to JPEG for vision APIs; preserve GIF as-is if animated is complex — flatten to JPEG."""
    img = Image.open(io.BytesIO(raw))
    if img.mode in ("RGBA", "P", "LA"):
        background = Image.new("RGB", img.size, (0, 0, 0))
        if img.mode == "P":
            img = img.convert("RGBA")
        background.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
        img = background
    elif img.mode != "RGB":
        img = img.convert("RGB")

    w, h = img.size
    scale = min(1.0, MAX_IMAGE_EDGE / max(w, h))
    if scale < 1.0:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, optimize=True)
    return buf.getvalue(), "image/jpeg"


def _extract_video_frames(video_path: Path, dest_dir: Path, count: int = VIDEO_FRAME_COUNT) -> list[Path]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise MediaError(
            "ffmpeg is not installed on this machine, so video frames cannot be extracted. "
            "Install ffmpeg, or attach still images instead."
        )
    # Sample evenly across the clip using fps filter relative to duration is hard without probing;
    # use fps≈count/duration via selecting frames at percentages with -ss seeks.
    frames: list[Path] = []
    # Probe duration
    duration = 0.0
    try:
        probe = subprocess.run(
            [
                shutil.which("ffprobe") or "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if probe.returncode == 0 and probe.stdout.strip():
            duration = float(probe.stdout.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        duration = 0.0

    timestamps: list[float]
    if duration > 1.0:
        timestamps = [duration * (i + 0.5) / count for i in range(count)]
    else:
        timestamps = [i * 0.5 for i in range(count)]

    for i, ts in enumerate(timestamps):
        out = dest_dir / f"frame_{i:02d}.jpg"
        try:
            result = subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-ss",
                    f"{ts:.3f}",
                    "-i",
                    str(video_path),
                    "-frames:v",
                    "1",
                    "-q:v",
                    "3",
                    str(out),
                ],
                capture_output=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MediaError(f"Failed to extract video frames: {exc}") from exc
        if result.returncode == 0 and out.exists() and out.stat().st_size > 0:
            frames.append(out)

    if not frames:
        raise MediaError("Could not extract any frames from the video.")
    return frames


def ingest_attachments(raw_items: list[dict[str, Any]]) -> tuple[list[StoredAttachment], list[dict[str, Any]]]:
    """Persist uploads and return (all stored records, vision content parts for the LLM).

    ``raw_items`` entries: ``{name, mime, data}`` where data is base64 (optionally data-URL prefixed).
    """
    if not raw_items:
        return [], []
    if len(raw_items) > MAX_ATTACHMENTS:
        raise MediaError(f"Too many attachments (max {MAX_ATTACHMENTS}).")

    _ensure_upload_dir()
    stored: list[StoredAttachment] = []
    vision_parts: list[dict[str, Any]] = []

    for item in raw_items:
        name = (item.get("name") or "attachment").strip() or "attachment"
        mime = (item.get("mime") or "application/octet-stream").split(";")[0].strip().lower()
        data_b64 = item.get("data") or ""
        if isinstance(data_b64, str) and data_b64.startswith("data:"):
            # data:image/png;base64,....
            try:
                header, data_b64 = data_b64.split(",", 1)
                if ";base64" in header and ":" in header:
                    mime = header.split(":")[1].split(";")[0].lower() or mime
            except ValueError:
                pass
        try:
            raw = base64.b64decode(data_b64, validate=False)
        except Exception as exc:  # noqa: BLE001
            raise MediaError(f"Invalid base64 for '{name}': {exc}") from exc
        if not raw:
            raise MediaError(f"Empty attachment: {name}")
        if len(raw) > MAX_BYTES:
            raise MediaError(f"'{name}' exceeds the {MAX_BYTES // (1024 * 1024)} MB limit.")

        att_id = uuid.uuid4().hex
        if mime in IMAGE_TYPES or mime.startswith("image/"):
            try:
                normalised, out_mime = _normalise_image_bytes(raw)
            except Exception as exc:  # noqa: BLE001
                raise MediaError(f"Could not read image '{name}': {exc}") from exc
            path = UPLOAD_DIR / f"{att_id}.jpg"
            path.write_bytes(normalised)
            rec = StoredAttachment(att_id, name, out_mime, "image", str(path))
            stored.append(rec)
            vision_parts.append(_image_part(normalised, out_mime))
        elif mime in VIDEO_TYPES or mime.startswith("video/"):
            ext = _guess_ext(mime, name)
            video_path = UPLOAD_DIR / f"{att_id}{ext}"
            video_path.write_bytes(raw)
            video_rec = StoredAttachment(att_id, name, mime, "video", str(video_path))
            stored.append(video_rec)

            with tempfile.TemporaryDirectory(prefix="jarvis-frames-") as tmp:
                frame_paths = _extract_video_frames(video_path, Path(tmp))
                frame_count = len(frame_paths)
                for fi, fp in enumerate(frame_paths):
                    frame_bytes = fp.read_bytes()
                    try:
                        normalised, out_mime = _normalise_image_bytes(frame_bytes)
                    except Exception:
                        normalised, out_mime = frame_bytes, "image/jpeg"
                    frame_id = f"{att_id}_f{fi}"
                    frame_path = UPLOAD_DIR / f"{frame_id}.jpg"
                    frame_path.write_bytes(normalised)
                    stored.append(
                        StoredAttachment(
                            frame_id,
                            f"{name} (frame {fi + 1})",
                            out_mime,
                            "frame",
                            str(frame_path),
                            parent_id=att_id,
                            note=f"Frame {fi + 1}/{frame_count} from video",
                        )
                    )
                    vision_parts.append(_image_part(normalised, out_mime))
            vision_parts.insert(
                0,
                {
                    "type": "text",
                    "text": (
                        f"[Attached video: {name}. {frame_count} representative frames "
                        f"follow for analysis — describe what you see across the sequence.]"
                    ),
                },
            )
        else:
            raise MediaError(f"Unsupported media type '{mime}' for '{name}'. Use images or video.")

    return stored, vision_parts


def _image_part(data: bytes, mime: str) -> dict[str, Any]:
    b64 = base64.b64encode(data).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{b64}"},
    }


def build_user_content(text: str, vision_parts: list[dict[str, Any]]) -> str | list[dict[str, Any]]:
    """OpenAI multimodal user content: plain string when no media, else content parts."""
    text = text.strip() or "Please analyse the attached media."
    if not vision_parts:
        return text
    instruction = (
        f"{text}\n\n"
        "[Vision instruction] The image(s) / frames below are NEW for THIS turn. "
        "Look at the actual pixels. Do not reuse descriptions of any earlier attachments "
        "or invent tags like <|start_of_image|>. Describe only what you see here."
    )
    parts: list[dict[str, Any]] = [{"type": "text", "text": instruction}]
    text_notes = [p for p in vision_parts if p.get("type") == "text"]
    images = [p for p in vision_parts if p.get("type") == "image_url"]
    return parts + text_notes + images


def vision_scan_content(vision_parts: list[dict[str, Any]], user_text: str = "") -> list[dict[str, Any]]:
    """Multimodal payload dedicated to a tool-free visual scan."""
    ask = (user_text or "").strip()
    focus = (
        f"The user asked: {ask}\n\n" if ask else ""
    ) + (
        "Describe what is visible in the newly attached media in concrete detail "
        "(subjects, text, UI, colours, layout, setting). "
        "Ignore any prior conversation about other images. "
        "Do not invent SMTP errors or other content that is not visible."
    )
    return build_user_content(focus, vision_parts)  # type: ignore[return-value]



def resolve_media_path(media_id: str) -> Path | None:
    """Find a stored upload by id (supports frame ids)."""
    _ensure_upload_dir()
    # Exact match any extension
    matches = list(UPLOAD_DIR.glob(f"{media_id}.*"))
    if matches:
        return matches[0]
    return None
