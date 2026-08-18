"""FastAPI application: REST config/conversation endpoints + the chat WebSocket."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from .agent import Agent
from .config import store
from .media import resolve_media_path
from .memory import Memory
from .rem import RemSleepService
from .stt import transcribe as stt_transcribe
from .tasks import init_manager
from .telegram_bot import TelegramBotService
from .tools import all_tools
from .tts import synthesize as tts_synthesize

memory = Memory()
task_manager = init_manager(memory)
rem_service = RemSleepService(memory)
_active_runs = 0
_ws_clients: set[WebSocket] = set()
telegram_bot = TelegramBotService(
    memory,
    rem_touch=lambda: rem_service.touch(),
    begin_run=lambda: _bump_runs(+1),
    end_run=lambda: _bump_runs(-1),
)


def _bump_runs(delta: int) -> None:
    global _active_runs
    _active_runs = max(0, _active_runs + delta)


@asynccontextmanager
async def lifespan(app: FastAPI):
    rem_service.set_busy_check(lambda: _active_runs > 0 or task_manager.active_count() > 0)

    async def _broadcast(event: dict) -> None:
        dead: list[WebSocket] = []
        for client in list(_ws_clients):
            try:
                await client.send_json(event)
            except Exception:  # noqa: BLE001
                dead.append(client)
        for client in dead:
            _ws_clients.discard(client)

    rem_service.on_event(_broadcast)
    task_manager.on_event(_broadcast)
    task_manager.fail_orphans()
    rem_service.start()
    await telegram_bot.start()
    yield
    await telegram_bot.stop()
    rem_service.stop()


app = FastAPI(title="JARVIS", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health() -> dict:
    return {"status": "online", "name": "JARVIS"}


@app.get("/api/config")
async def get_config() -> dict:
    cfg = store.as_dict()
    _redact(cfg)
    return cfg


class ConfigUpdate(BaseModel):
    data: dict[str, Any]


@app.post("/api/config")
async def update_config(update: ConfigUpdate) -> dict:
    data = _strip_masked(update.data)
    if not isinstance(data, dict):
        raise HTTPException(400, "Invalid config payload")
    prev_tg = store.as_dict().get("telegram") or {}
    store.update(data)
    new_tg = store.as_dict().get("telegram") or {}
    # Restart the bot when Telegram settings change (token / enable / allowlist).
    if _telegram_settings_changed(prev_tg, new_tg):
        await telegram_bot.reload()
    out = store.as_dict()
    _redact(out)
    return out


def _telegram_settings_changed(prev: dict, new: dict) -> bool:
    keys = ("enabled", "bot_token", "allowed_chat_ids", "allowed_user_ids", "notify_tools")
    for key in keys:
        if prev.get(key) != new.get(key):
            return True
    return False

@app.get("/api/tools")
async def list_tools() -> list[dict]:
    return [
        {"name": t.name, "description": t.description, "dangerous": bool(t.dangerous is True or callable(t.dangerous))}
        for t in all_tools()
    ]


@app.get("/api/conversations")
async def list_conversations() -> list[dict]:
    return memory.list_conversations()


@app.post("/api/conversations")
async def create_conversation() -> dict:
    cid = memory.create_conversation()
    return {"id": cid}


class Rename(BaseModel):
    title: str


@app.patch("/api/conversations/{cid}")
async def rename_conversation(cid: int, body: Rename) -> dict:
    memory.rename_conversation(cid, body.title)
    return {"ok": True}


@app.delete("/api/conversations/{cid}")
async def delete_conversation(cid: int) -> dict:
    memory.delete_conversation(cid)
    return {"ok": True}


@app.get("/api/conversations/{cid}/messages")
async def get_messages(cid: int) -> list[dict]:
    return memory.get_messages(cid)


class ActivityUpdate(BaseModel):
    items: list[dict[str, Any]]


@app.get("/api/conversations/{cid}/activity")
async def get_activity(cid: int) -> list[dict]:
    return memory.get_activity(cid)


@app.put("/api/conversations/{cid}/activity")
async def put_activity(cid: int, body: ActivityUpdate) -> dict:
    memory.replace_activity(cid, body.items)
    return {"ok": True, "count": len(body.items)}


@app.delete("/api/conversations/{cid}/activity")
async def delete_activity(cid: int) -> dict:
    memory.clear_activity(cid)
    return {"ok": True}


@app.get("/api/devices")
async def list_devices() -> list[dict]:
    return memory.list_devices()


@app.get("/api/tasks")
async def api_list_tasks(status: str | None = None) -> list[dict]:
    return task_manager.list_tasks(status)


@app.get("/api/tasks/{task_id}")
async def api_get_task(task_id: str) -> dict:
    task = task_manager.get(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    return task


@app.post("/api/tasks/{task_id}/cancel")
async def api_cancel_task(task_id: str) -> dict:
    if task_manager.cancel(task_id):
        return {"ok": True}
    if task_manager.get(task_id) is None:
        raise HTTPException(404, "Task not found")
    return {"ok": False, "reason": "Task is not running"}


@app.delete("/api/tasks/{task_id}")
async def api_delete_task(task_id: str) -> dict:
    if task_manager.get(task_id) is None:
        raise HTTPException(404, "Task not found")
    if not task_manager.delete(task_id):
        raise HTTPException(409, "Task is still running; cancel it first")
    return {"ok": True}


@app.get("/api/rem/status")
async def rem_status() -> dict:
    rem_service._sync_config()
    return rem_service.status.as_dict()


@app.post("/api/rem/run")
async def rem_run() -> dict:
    """Manually trigger a REM consolidation sweep."""
    return await rem_service.run_sweep(reason="manual")


@app.post("/api/rem/wake")
async def rem_wake() -> dict:
    """Wake JARVIS from REM / abort an in-flight dream cycle."""
    rem_service.wake()
    rem_service._sync_config()
    return rem_service.status.as_dict()


@app.get("/api/media/{media_id}")
async def get_media(media_id: str):
    """Serve a previously uploaded image/video by id."""
    if not media_id.replace("_", "").isalnum():
        raise HTTPException(404, "Not found")
    path = resolve_media_path(media_id)
    if path is None or not path.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(path)


class TtsRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)


@app.post("/api/tts")
async def tts_speak(body: TtsRequest):
    """Synthesize speech with the JARVIS butler voice (MP3). Browser-independent."""
    try:
        audio = await tts_synthesize(body.text)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"TTS failed: {exc}") from exc
    return Response(content=audio, media_type="audio/mpeg")


@app.post("/api/stt")
async def stt_listen(file: UploadFile = File(...)):
    """Transcribe recorded push-to-talk audio with local faster-whisper."""
    data = await file.read()
    filename = file.filename or "audio.webm"
    mime = file.content_type or "audio/webm"
    try:
        text = await stt_transcribe(data, filename=filename, mime=mime)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"STT failed: {exc}") from exc
    # Empty transcript is a soft miss (too quiet / too short), not a server error.
    return {"text": text or ""}


@app.websocket("/ws/chat")
async def chat_ws(ws: WebSocket) -> None:
    await ws.accept()
    _ws_clients.add(ws)
    loop = asyncio.get_event_loop()
    pending: dict[str, asyncio.Future] = {}
    active: dict[asyncio.Task, asyncio.Event] = {}
    global _active_runs

    async def emit(event: dict) -> None:
        try:
            await ws.send_json(event)
        except Exception:  # noqa: BLE001
            pass

    async def confirm(call: dict) -> bool:
        fut: asyncio.Future = loop.create_future()
        pending[call["id"]] = fut
        await emit({"type": "confirmation_request", **call})
        try:
            return await fut
        finally:
            pending.pop(call["id"], None)

    def interrupt_all() -> None:
        for fut in list(pending.values()):
            if not fut.done():
                fut.set_result(False)
        for task, cancel in list(active.items()):
            cancel.set()
            task.cancel()

    async def handle_message(
        conversation_id: int,
        text: str,
        cancel: asyncio.Event,
        attachments: list | None = None,
    ) -> None:
        global _active_runs
        rem_service.touch()
        _active_runs += 1
        agent = Agent(store.get(), memory)
        try:
            await agent.run(
                conversation_id,
                text,
                emit,
                confirm,
                cancel=cancel,
                attachments=attachments,
            )
        except asyncio.CancelledError:
            await emit({"type": "interrupted", "message": "Interrupted."})
            await emit({"type": "status", "state": "idle"})
            await emit({"type": "agent_end"})
        except Exception as exc:  # noqa: BLE001
            await emit({"type": "error", "message": f"Agent failure: {exc}"})
            await emit({"type": "agent_end"})
        finally:
            _active_runs = max(0, _active_runs - 1)
            rem_service.touch()

    try:
        while True:
            data = await ws.receive_json()
            mtype = data.get("type")
            if mtype == "user_message":
                interrupt_all()
                rem_service.touch()
                cid = data.get("conversation_id")
                if cid is None:
                    cid = memory.create_conversation()
                    await emit({"type": "conversation_created", "id": cid})
                cancel = asyncio.Event()
                task = asyncio.create_task(
                    handle_message(
                        int(cid),
                        data.get("text", ""),
                        cancel,
                        data.get("attachments") or None,
                    )
                )
                active[task] = cancel
                task.add_done_callback(lambda t: active.pop(t, None))
            elif mtype == "interrupt":
                interrupt_all()
                rem_service.touch()
            elif mtype == "confirm":
                rem_service.touch()
                call_id = data.get("call_id")
                fut = pending.get(call_id)
                if fut and not fut.done():
                    fut.set_result(bool(data.get("approved")))
            elif mtype == "ping":
                await emit({"type": "pong"})
    except WebSocketDisconnect:
        interrupt_all()
    finally:
        _ws_clients.discard(ws)


def _strip_masked(data: dict | list) -> dict | list:
    """Drop any field whose value is the redaction placeholder so real secrets survive."""
    if isinstance(data, list):
        return [_strip_masked(item) if isinstance(item, (dict, list)) else item for item in data]
    out: dict = {}
    for key, value in data.items():
        if isinstance(value, (dict, list)):
            out[key] = _strip_masked(value)
        elif value == "********":
            continue
        else:
            out[key] = value
    return out


def _redact(cfg: dict) -> None:
    """Mask secrets before returning config to the client."""
    llm = cfg.get("llm", {})
    for key in ("api_key", "fallback_api_key", "embedding_api_key"):
        if llm.get(key):
            llm[key] = "********"
    accounts = cfg.get("email_accounts") or {}
    for profile in accounts.get("profiles") or []:
        if not isinstance(profile, dict):
            continue
        for key in ("smtp_password", "imap_password"):
            if profile.get(key):
                profile[key] = "********"
                profile[f"{key}_configured"] = True
            else:
                profile[f"{key}_configured"] = False
    mail = cfg.get("email", {})
    for key in ("smtp_password", "imap_password"):
        if mail.get(key):
            mail[key] = "********"
            mail[f"{key}_configured"] = True
        else:
            mail[f"{key}_configured"] = False
    perms = cfg.setdefault("permissions", {})
    if perms.get("sudo_password"):
        perms["sudo_password"] = "********"
        perms["sudo_configured"] = True
    else:
        perms["sudo_configured"] = False
    tg = cfg.setdefault("telegram", {})
    if tg.get("bot_token"):
        tg["bot_token"] = "********"
        tg["bot_token_configured"] = True
    else:
        tg["bot_token_configured"] = False
