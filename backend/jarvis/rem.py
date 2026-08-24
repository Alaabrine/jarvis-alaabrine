"""REM sleep — inactivity-triggered memory consolidation (OpenClaw-inspired).

Phases per sweep (light → REM → deep):
  Light  — stage recent short-term signals
  REM    — reflect on themes / recurring ideas (LLM when available)
  Deep   — promote durable facts into long-term memory + MEMORY.md

Activated after a configurable idle period with no chat activity.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .config import DATA_DIR, store
from .llm import LLMClient, LLMError
from .memory import Memory

log = logging.getLogger("jarvis.rem")

MEMORY_MD = DATA_DIR / "MEMORY.md"
DREAMS_MD = DATA_DIR / "DREAMS.md"
DREAMS_STATE = DATA_DIR / "memory" / ".dreams"
PHASE_SIGNALS = DREAMS_STATE / "phase-signals.json"


def read_memory_files() -> dict[str, str]:
    """Return consolidated long-term markdown files for the memory bank UI."""
    out: dict[str, str] = {}
    for path, key in ((MEMORY_MD, "memory_md"), (DREAMS_MD, "dreams_md")):
        try:
            out[key] = path.read_text(encoding="utf-8") if path.is_file() else ""
        except OSError:
            out[key] = ""
    return out


def wipe_memory_files() -> None:
    """Clear consolidated markdown memory files."""
    for path in (MEMORY_MD, DREAMS_MD):
        try:
            if path.is_file():
                path.write_text("", encoding="utf-8")
        except OSError:
            log.warning("Failed to wipe %s", path)
    try:
        if PHASE_SIGNALS.is_file():
            PHASE_SIGNALS.unlink()
    except OSError:
        log.warning("Failed to remove phase signals")


@dataclass
class RemStatus:
    enabled: bool = True
    phase: str = "awake"  # awake | light | rem | deep | dreaming
    last_activity_at: float = field(default_factory=time.time)
    last_sweep_at: float | None = None
    last_result: str = ""
    idle_minutes: int = 15
    min_interval_minutes: int = 60
    running: bool = False

    def as_dict(self) -> dict[str, Any]:
        idle_for = max(0.0, time.time() - self.last_activity_at)
        return {
            "enabled": self.enabled,
            "phase": self.phase,
            "running": self.running,
            "idle_seconds": int(idle_for),
            "idle_minutes_config": self.idle_minutes,
            "min_interval_minutes": self.min_interval_minutes,
            "last_activity_at": self.last_activity_at,
            "last_sweep_at": self.last_sweep_at,
            "last_result": self.last_result,
            "seconds_until_eligible": max(
                0,
                int(self.idle_minutes * 60 - idle_for),
            ),
        }


class RemSleepService:
    def __init__(self, memory: Memory) -> None:
        self.memory = memory
        self.status = RemStatus()
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._busy_check: Callable[[], bool] | None = None
        self._listeners: list[Callable[[dict], Any]] = []
        self._cancel_sweep = False
        self._sync_config()

    def set_busy_check(self, fn: Callable[[], bool]) -> None:
        """Return True when an agent run is in progress (skip dreaming)."""
        self._busy_check = fn

    def on_event(self, fn: Callable[[dict], Any]) -> None:
        self._listeners.append(fn)

    async def _emit(self, event: dict) -> None:
        for fn in list(self._listeners):
            try:
                result = fn(event)
                if hasattr(result, "__await__"):
                    await result
            except Exception:  # noqa: BLE001
                pass

    def _sync_config(self) -> None:
        cfg = store.get()
        rem = cfg.rem
        self.status.enabled = bool(rem.enabled)
        self.status.idle_minutes = max(1, int(rem.idle_minutes))
        self.status.min_interval_minutes = max(5, int(rem.min_interval_minutes))

    def touch(self) -> None:
        self.status.last_activity_at = time.time()
        if self.status.phase not in {"light", "rem", "deep", "dreaming"}:
            self.status.phase = "awake"

    def wake(self) -> None:
        """User-initiated wake: reset idle timer and abort an in-flight dream cycle."""
        self.touch()
        self._cancel_sweep = True
        if not self.status.running:
            self.status.phase = "awake"

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="jarvis-rem-sleep")

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(20)
                self._sync_config()
                if not self.status.enabled or self.status.running:
                    continue
                if self._busy_check and self._busy_check():
                    continue
                idle = time.time() - self.status.last_activity_at
                if idle < self.status.idle_minutes * 60:
                    continue
                if self.status.last_sweep_at:
                    since = time.time() - self.status.last_sweep_at
                    if since < self.status.min_interval_minutes * 60:
                        continue
                await self.run_sweep(reason="inactivity")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("REM loop error: %s", exc)

    async def run_sweep(self, reason: str = "manual") -> dict[str, Any]:
        async with self._lock:
            if self.status.running:
                return {"ok": False, "message": "Already dreaming."}
            self._sync_config()
            self.status.running = True
            self.status.phase = "dreaming"
            self._cancel_sweep = False
            await self._emit({"type": "rem_status", **self.status.as_dict()})
            await self._log(
                "dreaming",
                "info",
                f"REM cycle started ({reason}). Softening arc reactor…",
            )

            try:
                DREAMS_STATE.mkdir(parents=True, exist_ok=True)
                report: dict[str, Any] = {"reason": reason, "phases": {}}

                # --- Light ---
                self.status.phase = "light"
                await self._emit({"type": "rem_status", **self.status.as_dict()})
                await self._log("light", "info", "Light sleep — gathering short-term signals…")
                staged = self._phase_light()
                report["phases"]["light"] = {"staged": len(staged), "samples": staged[:8]}
                await self._log(
                    "light",
                    "info",
                    f"Staged {len(staged)} short-term traces for consolidation.",
                )
                for sample in staged[:12]:
                    await self._log("light", "entry", sample)
                    await asyncio.sleep(0.04)
                    if self._cancel_sweep:
                        return await self._abort_sweep("Woken during light sleep.")

                if self._cancel_sweep:
                    return await self._abort_sweep("Woken during light sleep.")

                # --- REM ---
                self.status.phase = "rem"
                await self._emit({"type": "rem_status", **self.status.as_dict()})
                await self._log("rem", "info", "REM — reflecting on recurring themes…")
                themes = await self._phase_rem(staged)
                report["phases"]["rem"] = {"themes": themes}
                if themes:
                    await self._log("rem", "info", f"Extracted {len(themes)} dream themes.")
                    for theme in themes:
                        await self._log("rem", "entry", theme)
                        await asyncio.sleep(0.04)
                else:
                    await self._log("rem", "info", "No themes surfaced this cycle.")

                if self._cancel_sweep:
                    return await self._abort_sweep("Woken during REM.")

                # --- Deep ---
                self.status.phase = "deep"
                await self._emit({"type": "rem_status", **self.status.as_dict()})
                await self._log("deep", "info", "Deep sleep — promoting durable memories…")
                promoted = await self._phase_deep(staged, themes)
                report["phases"]["deep"] = {"promoted": promoted}
                if promoted:
                    await self._log(
                        "deep",
                        "info",
                        f"Promoted {len(promoted)} entries to long-term memory.",
                    )
                    for item in promoted:
                        await self._log("deep", "entry", item)
                        await asyncio.sleep(0.04)
                else:
                    await self._log("deep", "info", "Nothing met the promotion threshold.")

                if self._cancel_sweep:
                    return await self._abort_sweep("Woken during deep sleep.")

                self._append_dreams_diary(reason, themes, promoted)
                self.status.last_sweep_at = time.time()
                self.status.last_result = (
                    f"Light staged {len(staged)}; REM themes {len(themes)}; "
                    f"Deep promoted {len(promoted)}."
                )
                self.status.phase = "awake"
                # Don't reset idle timer completely — mark activity lightly so we don't loop
                self.status.last_activity_at = time.time()
                result = {"ok": True, "message": self.status.last_result, "report": report}
                await self._log("awake", "info", f"Dream cycle complete. {self.status.last_result}")
                await self._emit({"type": "rem_status", **self.status.as_dict(), "result": result})
                return result
            except Exception as exc:  # noqa: BLE001
                self.status.phase = "awake"
                self.status.last_result = f"Dreaming failed: {exc}"
                await self._log("awake", "error", self.status.last_result)
                await self._emit({"type": "rem_status", **self.status.as_dict()})
                return {"ok": False, "message": str(exc)}
            finally:
                self.status.running = False
                self._cancel_sweep = False
                if self.status.phase in {"light", "rem", "deep", "dreaming"}:
                    self.status.phase = "awake"

    async def _abort_sweep(self, message: str) -> dict[str, Any]:
        self.status.phase = "awake"
        self.status.last_result = message
        await self._log("awake", "info", message)
        await self._emit({"type": "rem_status", **self.status.as_dict()})
        return {"ok": False, "message": message}

    async def _log(self, phase: str, level: str, text: str) -> None:
        await self._emit(
            {
                "type": "rem_log",
                "phase": phase,
                "level": level,
                "text": text,
                "ts": time.time(),
            }
        )

    def _phase_light(self) -> list[str]:
        """Stage recent short-term conversation / device memories."""
        rows = self.memory.list_memories(
            kinds=["conversation", "device", "peripheral", "short_term"], limit=80
        )
        staged: list[str] = []
        seen: set[str] = set()
        for row in rows:
            text = (row.get("text") or "").strip()
            if len(text) < 24:
                continue
            key = text[:120].lower()
            if key in seen:
                continue
            seen.add(key)
            staged.append(text)
            self.memory.add_memory("staged", text, None)

        # Also pull recent chat snippets across sessions.
        for conv in self.memory.list_conversations()[:12]:
            msgs = self.memory.get_messages(int(conv["id"]))[-6:]
            for m in msgs:
                if m["role"] != "user":
                    continue
                snippet = (m.get("content") or "").strip()
                if len(snippet) < 16:
                    continue
                line = f"User interest: {snippet[:240]}"
                key = line[:120].lower()
                if key in seen:
                    continue
                seen.add(key)
                staged.append(line)

        self._write_phase_block("Light Sleep", staged[:20])
        return staged[:40]

    async def _phase_rem(self, staged: list[str]) -> list[str]:
        """Reflect on themes — LLM when possible, else heuristic keywords."""
        if not staged:
            self._write_phase_block("REM Sleep", ["No short-term material to dream about."])
            return []

        corpus = "\n".join(f"- {s}" for s in staged[:30])
        themes: list[str] = []
        cfg = store.get()
        llm = LLMClient(cfg.llm)
        prompt = (
            "You are consolidating JARVIS short-term memories during REM sleep.\n"
            "From the notes below, extract 3-8 recurring themes or durable facts about the user, "
            "their devices, preferences, or ongoing projects.\n"
            "Skip one-off commands (open a URL, launch an app) unless they clearly recur.\n"
            "Return ONLY a JSON array of short strings. No markdown.\n\n"
            f"Notes:\n{corpus}"
        )
        try:
            msg = await llm.chat(
                [
                    {"role": "system", "content": "Return concise JSON only."},
                    {"role": "user", "content": prompt},
                ],
                tools=None,
                temperature=0.3,
            )
            raw = (msg.content or "").strip()
            themes = _parse_json_string_list(raw)
        except (LLMError, Exception) as exc:  # noqa: BLE001
            log.info("REM LLM unavailable (%s); using heuristic themes", exc)
            themes = _heuristic_themes(staged)

        if not themes:
            themes = _heuristic_themes(staged)

        for t in themes:
            self.memory.add_memory("rem_theme", t, None)
        self._write_phase_block("REM Sleep", themes)
        self._record_phase_signal("rem", themes)
        return themes

    async def _phase_deep(self, staged: list[str], themes: list[str]) -> list[str]:
        """Promote durable candidates into long-term MEMORY.md + DB."""
        candidates = list(themes)
        # Reinforce staged lines that overlap themes.
        theme_terms = {w.lower() for t in themes for w in re.findall(r"[a-zA-Z]{4,}", t)}
        for s in staged:
            if _is_ephemeral_memory_line(s):
                continue
            score = sum(1 for w in theme_terms if w in s.lower())
            if score >= 2 or len(s) > 80 and score >= 1:
                candidates.append(s)

        # Dedupe
        promoted: list[str] = []
        seen: set[str] = set()
        existing = self._read_memory_md()
        for c in candidates:
            if _is_ephemeral_memory_line(c):
                continue
            norm = re.sub(r"\s+", " ", c.strip().lower())
            if len(norm) < 20 or norm in seen:
                continue
            if any(norm[:60] in e.lower() for e in existing):
                continue
            seen.add(norm)
            promoted.append(c.strip())
            if len(promoted) >= 8:
                break

        if promoted:
            self._append_memory_md(promoted)
            for p in promoted:
                emb = None
                try:
                    emb = await LLMClient(store.get().llm).embed(p)
                except Exception:  # noqa: BLE001
                    pass
                self.memory.add_memory("long_term", p, emb)

        self._write_phase_block("Deep Sleep", promoted or ["Nothing met the promotion threshold."])
        return promoted

    def _read_memory_md(self) -> list[str]:
        if not MEMORY_MD.exists():
            return []
        lines = []
        for line in MEMORY_MD.read_text(errors="replace").splitlines():
            line = line.strip()
            if line.startswith("- "):
                lines.append(line[2:].strip())
        return lines

    def _append_memory_md(self, entries: list[str]) -> None:
        MEMORY_MD.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        block = [f"\n## Consolidated {stamp}\n"]
        for e in entries:
            block.append(f"- {e}\n")
        with MEMORY_MD.open("a", encoding="utf-8") as f:
            f.writelines(block)

    def _write_phase_block(self, title: str, lines: list[str]) -> None:
        DREAMS_MD.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        body = "\n".join(f"- {l}" for l in lines) if lines else "- (empty)"
        with DREAMS_MD.open("a", encoding="utf-8") as f:
            f.write(f"\n## {title} — {stamp}\n{body}\n")

    def _append_dreams_diary(self, reason: str, themes: list[str], promoted: list[str]) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        with DREAMS_MD.open("a", encoding="utf-8") as f:
            f.write(
                f"\n## Dream Diary — {stamp}\n"
                f"Trigger: {reason}. Themes reflected: {len(themes)}. "
                f"Promoted to long-term: {len(promoted)}.\n"
            )

    def _record_phase_signal(self, phase: str, items: list[str]) -> None:
        DREAMS_STATE.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {}
        if PHASE_SIGNALS.exists():
            try:
                data = json.loads(PHASE_SIGNALS.read_text())
            except json.JSONDecodeError:
                data = {}
        bucket = data.setdefault(phase, [])
        bucket.append({"ts": time.time(), "items": items[:12]})
        data[phase] = bucket[-20:]
        PHASE_SIGNALS.write_text(json.dumps(data, indent=2))


def _parse_json_string_list(raw: str) -> list[str]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    except json.JSONDecodeError:
        pass
    # Fallback: bullet lines
    return [ln.lstrip("-* ").strip() for ln in raw.splitlines() if ln.strip().startswith(("-", "*"))]


def _heuristic_themes(staged: list[str]) -> list[str]:
    freq: dict[str, int] = {}
    for s in staged:
        if _is_ephemeral_memory_line(s):
            # Still count terms for recurrence detection, but skip promoting the raw line.
            pass
        for w in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", s.lower()):
            if w in {"user", "asked", "jarvis", "answered", "this", "that", "with", "from", "have", "interest", "open", "please"}:
                continue
            freq[w] = freq.get(w, 0) + 1
    top = sorted(freq.items(), key=lambda x: x[1], reverse=True)[:8]
    return [f"Recurring interest: {w} (seen {n}×)" for w, n in top if n >= 2][:6]


def _is_ephemeral_memory_line(text: str) -> bool:
    """One-off chat actions — fine for dreaming, not for durable MEMORY.md."""
    low = (text or "").strip().lower()
    if not low:
        return True
    if low.startswith("user asked:") or low.startswith("user interest:"):
        return True
    if re.match(r"^(open|launch|go to|visit)\b", low):
        return True
    return False
