"""Telegram Bot bridge — talk to JARVIS remotely via the Bot API.

Uses outbound long-polling (no need to expose the local API). Messages reuse the
same Agent.run pipeline as the web UI, including tools, memory, and confirmations
(via inline Approve/Deny buttons).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path
from typing import Any, Callable

import httpx

from .agent import Agent, RunControl
from .config import DATA_DIR, store
from .memory import Memory

log = logging.getLogger("jarvis.telegram")

TELEGRAM_API = "https://api.telegram.org"
SESSIONS_PATH = DATA_DIR / "telegram_sessions.json"
MAX_MESSAGE_LEN = 4000
CONFIRM_TIMEOUT_S = 300


class TelegramBotService:
    """Lifecycle-managed Telegram long-poller wired into the JARVIS agent."""

    def __init__(
        self,
        memory: Memory,
        *,
        rem_touch: Callable[[], None],
        begin_run: Callable[[], None],
        end_run: Callable[[], None],
    ) -> None:
        self.memory = memory
        self._rem_touch = rem_touch
        self._begin_run = begin_run
        self._end_run = end_run
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._client: httpx.AsyncClient | None = None
        self._offset = 0
        self._sessions: dict[str, int] = {}
        self._active: dict[int, tuple[asyncio.Task, RunControl]] = {}
        self._pending_confirm: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    # --- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        await self.reload()

    async def stop(self) -> None:
        self._stop.set()
        self._interrupt_all()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        if self._client:
            await self._client.aclose()
            self._client = None

    async def reload(self) -> None:
        """Restart the poller from the current config (noop if disabled / no token)."""
        await self.stop()
        self._stop = asyncio.Event()
        cfg = store.get().telegram
        if not cfg.enabled or not (cfg.bot_token or "").strip():
            log.info("Telegram bot idle (disabled or missing token).")
            return
        if not cfg.allowed_chat_ids and not cfg.allowed_user_ids:
            log.warning(
                "Telegram bot enabled but no allowed_chat_ids / allowed_user_ids — "
                "all chats will be rejected. Set an allowlist (use /whoami)."
            )
        self._load_sessions()
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0))
        self._task = asyncio.create_task(self._poll_loop(), name="jarvis-telegram")
        log.info("Telegram bot started.")

    # --- sessions --------------------------------------------------------

    def _load_sessions(self) -> None:
        self._sessions = {}
        if not SESSIONS_PATH.exists():
            return
        try:
            data = json.loads(SESSIONS_PATH.read_text())
            if isinstance(data, dict):
                self._sessions = {str(k): int(v) for k, v in data.items()}
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            self._sessions = {}

    def _save_sessions(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SESSIONS_PATH.write_text(json.dumps(self._sessions, indent=2))

    def _conversation_for(self, chat_id: int, *, fresh: bool = False) -> int:
        key = str(chat_id)
        if not fresh and key in self._sessions:
            return self._sessions[key]
        cid = self.memory.create_conversation(title=f"Telegram {chat_id}")
        self._sessions[key] = cid
        self._save_sessions()
        return cid

    # --- auth ------------------------------------------------------------

    def _is_allowed(self, chat_id: int, user_id: int | None) -> bool:
        cfg = store.get().telegram
        chats = {str(x) for x in cfg.allowed_chat_ids}
        users = {str(x) for x in cfg.allowed_user_ids}
        if not chats and not users:
            return False
        if str(chat_id) in chats:
            return True
        if user_id is not None and str(user_id) in users:
            return True
        return False

    # --- Telegram HTTP ---------------------------------------------------

    def _api_url(self, method: str) -> str:
        token = store.get().telegram.bot_token.strip()
        return f"{TELEGRAM_API}/bot{token}/{method}"

    async def _api(self, method: str, payload: dict | None = None, *, files: dict | None = None) -> Any:
        assert self._client is not None
        url = self._api_url(method)
        if files:
            resp = await self._client.post(url, data=payload or {}, files=files)
        else:
            resp = await self._client.post(url, json=payload or {})
        resp.raise_for_status()
        body = resp.json()
        if not body.get("ok"):
            raise RuntimeError(body.get("description") or f"Telegram API error: {method}")
        return body.get("result")

    async def _send_text(self, chat_id: int, text: str, **extra: Any) -> Any:
        chunks = _chunk_text(text or "(empty reply)")
        last = None
        for i, chunk in enumerate(chunks):
            payload = {"chat_id": chat_id, "text": chunk, **(extra if i == 0 else {})}
            try:
                last = await self._api("sendMessage", payload)
            except Exception:
                # Telegram rejects messages when Markdown entities are malformed.
                payload.pop("parse_mode", None)
                last = await self._api("sendMessage", payload)
        return last

    async def _typing(self, chat_id: int) -> None:
        try:
            await self._api("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except Exception:  # noqa: BLE001
            pass

    async def _download_file(self, file_id: str) -> tuple[bytes, str]:
        info = await self._api("getFile", {"file_id": file_id})
        path = info.get("file_path") or ""
        token = store.get().telegram.bot_token.strip()
        url = f"{TELEGRAM_API}/file/bot{token}/{path}"
        assert self._client is not None
        resp = await self._client.get(url)
        resp.raise_for_status()
        name = Path(path).name or "telegram.bin"
        return resp.content, name

    # --- poll loop -------------------------------------------------------

    async def _poll_loop(self) -> None:
        assert self._client is not None
        # Drop pending updates so we don't replay a backlog on restart.
        try:
            await self._api("getUpdates", {"offset": -1, "timeout": 0})
        except Exception as exc:  # noqa: BLE001
            log.warning("Telegram getUpdates warm-up failed: %s", exc)

        while not self._stop.is_set():
            try:
                updates = await self._api(
                    "getUpdates",
                    {"offset": self._offset, "timeout": 25, "allowed_updates": ["message", "callback_query"]},
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if self._stop.is_set():
                    break
                log.warning("Telegram poll error: %s", exc)
                await asyncio.sleep(3)
                continue

            for update in updates or []:
                self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
                try:
                    await self._handle_update(update)
                except Exception as exc:  # noqa: BLE001
                    log.exception("Telegram update failed: %s", exc)

    async def _handle_update(self, update: dict) -> None:
        if "callback_query" in update:
            await self._handle_callback(update["callback_query"])
            return
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return
        chat = msg.get("chat") or {}
        chat_id = int(chat["id"])
        from_user = msg.get("from") or {}
        user_id = int(from_user["id"]) if from_user.get("id") is not None else None
        text = (msg.get("text") or msg.get("caption") or "").strip()

        # /whoami always answers (helps set the allowlist) even when denied.
        if text.startswith("/whoami"):
            await self._send_text(
                chat_id,
                f"chat_id: `{chat_id}`\nuser_id: `{user_id}`\n"
                "Add one of these to JARVIS_TELEGRAM_ALLOWED_CHAT_IDS or "
                "JARVIS_TELEGRAM_ALLOWED_USER_IDS (or Settings → Telegram).",
                parse_mode="Markdown",
            )
            return

        if not self._is_allowed(chat_id, user_id):
            await self._send_text(
                chat_id,
                "Access denied. This JARVIS bot is locked to an allowlist. "
                "Send /whoami and add your id in Settings → Telegram.",
            )
            return

        if text.startswith("/start") or text.startswith("/help"):
            await self._send_text(chat_id, _help_text())
            return
        if text.startswith("/new"):
            cid = self._conversation_for(chat_id, fresh=True)
            await self._send_text(chat_id, f"New conversation started (#{cid}). How may I assist you?")
            return
        if text.startswith("/cancel") or text.startswith("/stop"):
            self._interrupt_chat(chat_id)
            await self._send_text(chat_id, "Interrupted.")
            return
        if text.startswith("/status"):
            busy = chat_id in self._active
            cid = self._sessions.get(str(chat_id))
            await self._send_text(
                chat_id,
                f"Status: {'busy' if busy else 'idle'}\n"
                f"Conversation: {cid or 'none yet'}",
            )
            return

        attachments = await self._extract_attachments(msg)
        if not text and not attachments:
            return
        if not text:
            text = "Please look at the attached media."

        await self._run_agent(chat_id, text, attachments)

    async def _extract_attachments(self, msg: dict) -> list[dict] | None:
        out: list[dict] = []
        photos = msg.get("photo") or []
        if photos:
            # Largest size last.
            best = photos[-1]
            try:
                data, name = await self._download_file(best["file_id"])
                out.append(
                    {
                        "name": name if "." in name else f"{name}.jpg",
                        "mime": "image/jpeg",
                        "data": base64.b64encode(data).decode("ascii"),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Failed to download Telegram photo: %s", exc)

        doc = msg.get("document")
        if doc and (doc.get("mime_type") or "").startswith("image/"):
            try:
                data, name = await self._download_file(doc["file_id"])
                out.append(
                    {
                        "name": doc.get("file_name") or name,
                        "mime": doc.get("mime_type") or "image/jpeg",
                        "data": base64.b64encode(data).decode("ascii"),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Failed to download Telegram document: %s", exc)

        return out or None

    async def _handle_callback(self, cq: dict) -> None:
        data = cq.get("data") or ""
        cq_id = cq.get("id")
        msg = cq.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = int(chat["id"]) if chat.get("id") is not None else None
        from_user = cq.get("from") or {}
        user_id = int(from_user["id"]) if from_user.get("id") is not None else None

        if chat_id is None or not self._is_allowed(chat_id, user_id):
            if cq_id:
                await self._api("answerCallbackQuery", {"callback_query_id": cq_id, "text": "Denied"})
            return

        approved: bool | None = None
        call_id = ""
        if data.startswith("tgok:"):
            approved = True
            call_id = data[5:]
        elif data.startswith("tgno:"):
            approved = False
            call_id = data[5:]

        if approved is None or not call_id:
            if cq_id:
                await self._api("answerCallbackQuery", {"callback_query_id": cq_id})
            return

        fut = self._pending_confirm.get(call_id)
        if fut and not fut.done():
            fut.set_result(approved)

        if cq_id:
            await self._api(
                "answerCallbackQuery",
                {"callback_query_id": cq_id, "text": "Approved" if approved else "Denied"},
            )
        # Flatten the keyboard so the buttons can't be pressed twice.
        try:
            await self._api(
                "editMessageReplyMarkup",
                {
                    "chat_id": chat_id,
                    "message_id": msg.get("message_id"),
                    "reply_markup": {"inline_keyboard": []},
                },
            )
            await self._api(
                "editMessageText",
                {
                    "chat_id": chat_id,
                    "message_id": msg.get("message_id"),
                    "text": (msg.get("text") or "Confirmation")
                    + ("\n\n✅ Approved." if approved else "\n\n❌ Denied."),
                },
            )
        except Exception:  # noqa: BLE001
            pass

    # --- agent run -------------------------------------------------------

    def _interrupt_all(self) -> None:
        for fut in list(self._pending_confirm.values()):
            if not fut.done():
                fut.set_result(False)
        for chat_id in list(self._active):
            self._interrupt_chat(chat_id)

    def _interrupt_chat(self, chat_id: int) -> None:
        entry = self._active.get(chat_id)
        if not entry:
            return
        task, control = entry
        control.interrupt()
        task.cancel()

    async def _run_agent(self, chat_id: int, text: str, attachments: list[dict] | None) -> None:
        async with self._lock:
            existing = self._active.get(chat_id)
            if existing is not None and not attachments:
                _task, control = existing
                if not _task.done():
                    control.inject(text)
                    return
            self._interrupt_chat(chat_id)

        cid = self._conversation_for(chat_id)
        control = RunControl()
        control.conversation_id = cid
        task = asyncio.create_task(
            self._handle_message(chat_id, cid, text, control, attachments),
            name=f"tg-agent-{chat_id}",
        )
        self._active[chat_id] = (task, control)

        def _done(t: asyncio.Task, c: int = chat_id) -> None:
            self._active.pop(c, None)

        task.add_done_callback(_done)

    async def _handle_message(
        self,
        chat_id: int,
        conversation_id: int,
        text: str,
        control: RunControl,
        attachments: list[dict] | None,
    ) -> None:
        self._rem_touch()
        self._begin_run()
        await self._typing(chat_id)
        typing_task = asyncio.create_task(self._typing_heartbeat(chat_id, control.cancel))

        notify = bool(store.get().telegram.notify_tools)

        async def emit(event: dict) -> None:
            etype = event.get("type")
            if etype == "assistant":
                await self._send_text(chat_id, event.get("text") or "")
            elif etype == "say":
                await self._send_text(chat_id, event.get("text") or "")
            elif etype == "suggestions":
                items = event.get("items") or []
                lines = [
                    f"• {item.get('label')}: {item.get('prompt')}"
                    for item in items
                    if isinstance(item, dict) and (item.get("label") or item.get("prompt"))
                ]
                if lines:
                    await self._send_text(chat_id, "If you want, I can also:\n" + "\n".join(lines))
            elif etype == "error":
                await self._send_text(chat_id, f"⚠️ {event.get('message') or 'Error'}")
            elif etype == "interrupted":
                await self._send_text(chat_id, event.get("message") or "Interrupted.")
            elif etype == "status" and notify:
                state = event.get("state")
                if state == "awaiting_confirmation":
                    return
                label = {
                    "thinking": "Thinking…",
                    "running_tool": "Running a tool…",
                    "idle": None,
                }.get(state or "", state)
                if label:
                    await self._typing(chat_id)
            elif etype == "tool_call" and notify:
                name = event.get("name") or "tool"
                danger = " ⚠️ gated" if event.get("dangerous") and not event.get("auto_approved") else ""
                await self._send_text(chat_id, f"⚙️ `{name}`{danger}", parse_mode="Markdown")
            elif etype == "tool_result" and notify:
                ok = event.get("ok")
                name = event.get("name") or "tool"
                mark = "✓" if ok else "✗"
                out = (event.get("output") or "").strip().replace("`", "'")
                if len(out) > 280:
                    out = out[:277] + "…"
                body = f"{mark} `{name}`"
                if out:
                    body += f"\n```\n{out}\n```"
                await self._send_text(chat_id, body, parse_mode="Markdown")

        async def confirm(call: dict) -> bool:
            preview = call.get("preview") or json.dumps(call.get("args") or {}, indent=2)[:1500]
            name = call.get("name") or "action"
            risk = call.get("risk") or "This is irreversible."
            call_id = str(call.get("id") or "")
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "✅ Approve", "callback_data": f"tgok:{call_id}"},
                        {"text": "❌ Deny", "callback_data": f"tgno:{call_id}"},
                    ]
                ]
            }
            await self._send_text(
                chat_id,
                f"{risk}\n`{name}`\n\n{preview}",
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            loop = asyncio.get_running_loop()
            fut: asyncio.Future = loop.create_future()
            self._pending_confirm[call_id] = fut
            try:
                return await asyncio.wait_for(fut, timeout=CONFIRM_TIMEOUT_S)
            except asyncio.TimeoutError:
                await self._send_text(chat_id, f"Timed out waiting to approve `{name}` — denied.", parse_mode="Markdown")
                return False
            finally:
                self._pending_confirm.pop(call_id, None)

        agent = Agent(store.get(), self.memory)
        try:
            await agent.run(
                conversation_id,
                text,
                emit,
                confirm,
                control=control,
                attachments=attachments,
            )
        except asyncio.CancelledError:
            await emit({"type": "interrupted", "message": "Interrupted."})
        except Exception as exc:  # noqa: BLE001
            log.exception("Telegram agent failure")
            await emit({"type": "error", "message": f"Agent failure: {exc}"})
        finally:
            control.interrupt()
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass
            self._end_run()
            self._rem_touch()

    async def _typing_heartbeat(self, chat_id: int, cancel: asyncio.Event) -> None:
        while not cancel.is_set():
            await self._typing(chat_id)
            try:
                await asyncio.wait_for(cancel.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                continue


def _chunk_text(text: str, limit: int = MAX_MESSAGE_LEN) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    return chunks


def _help_text() -> str:
    return (
        "JARVIS is online via Telegram.\n\n"
        "Commands:\n"
        "/new — start a fresh conversation\n"
        "/cancel — interrupt the current run\n"
        "/status — busy / conversation info\n"
        "/whoami — show your chat_id and user_id\n"
        "/help — this message\n\n"
        "Send text or a photo (with optional caption) at any time — including while "
        "JARVIS is working, to elaborate, follow up, or redirect. "
        "Say stop or /cancel to abort. Irreversible actions (delete, overwrite, "
        "shutdown/reboot, send email) will ask you to Approve / Deny here."
    )
