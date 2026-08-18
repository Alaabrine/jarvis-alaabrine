# JARVIS

A self-hosted, butler-style AI agent inspired by JARVIS from Iron Man.

- **Local-first LLM** via [Odysseus](https://odysseus-dev.github.io/odysseus/) (any OpenAI-compatible endpoint), with a cloud API fallback.
- **Full device control**: JARVIS learns your machine autonomously on startup (hardware, OS, desktop, control utilities) and acts on natural-language requests via unified control — no need to pick explicit tools or run a system scan.
- **Full internet access**: web search + page reading so it can learn to answer your prompts.
- **Persistent memory**: chat sessions, activity monitor history, and device profiles survive restarts; REM sleep consolidates short-term memories when idle.
- **Neon-tech web UI** streaming everything JARVIS is thinking and doing in real time.
- **Voice** interaction (local faster-whisper STT; butler-style neural TTS from the backend).
- **Desktop app** (Tauri) with a configurable global hotkey to summon JARVIS, plus
  **push-to-talk** (hold Super+\`) to dictate prompts from anywhere.
- **Telegram bot** for remote chat from your phone (outbound long-poll; laptop stays private).

> JARVIS can run shell commands, modify files, and send email on your behalf.
> Destructive actions require explicit confirmation in the UI. The backend binds to
> `127.0.0.1` only. Use at your own risk on a machine you control.



## Project layout

```
backend/   FastAPI agent server (agent loop, tools, memory, LLM client)
web/       React + Vite neon UI (chat, live activity feed, confirmations, voice)
desktop/   Tauri app wrapping the web UI + global hotkey
```



## Quick start



### 1. Backend

```bash
cd backend
uv sync                     # or: pip install -e .
uv run jarvis               # starts the API on http://127.0.0.1:8787
```

Configure the model in the web UI Settings, or via environment variables:

```bash
3"http://localhost:11434/v1"   # Odysseus / Ollama / vLLM / llama.cpp
export JARVIS_LLM_API_KEY="not-needed-for-local"
export JARVIS_LLM_MODEL="qwen2.5:latest"
# Optional cloud fallback if the local endpoint is unreachable:
export JARVIS_FALLBACK_BASE_URL="https://api.openai.com/v1"
export JARVIS_FALLBACK_API_KEY="sk-..."
export JARVIS_FALLBACK_MODEL="gpt-4o-mini"
```



### 2. Web UI

```bash
cd web
npm install
npm run dev                 # http://localhost:5173
```



### 3. Desktop app (optional)

```bash
cd desktop
npm install
npm run tauri dev
```

The global hotkey (default `Ctrl+Alt+J`) toggles the JARVIS window from anywhere.

Hold **Super+`** (backtick) to push-to-talk: JARVIS shows/focuses, listens while held,
and sends your speech as a prompt when you release. Override with `JARVIS_PTT_HOTKEY`
(default `Super+Backquote`).

## Connecting to Odysseus (local model)

1. Install and run [Odysseus](https://odysseus-dev.github.io/odysseus/) and serve a model
  (via its Cookbook / Ollama / vLLM / llama.cpp).
2. Point JARVIS at the OpenAI-compatible endpoint it exposes, e.g.
  `http://localhost:11434/v1`, and set the model name to the one you're serving.
3. Optionally set a cloud fallback (OpenAI / OpenRouter / Anthropic-compatible) so JARVIS
  keeps working if the local endpoint is down.



## Voice

- Speak to JARVIS with the microphone button in the composer (browser Web Speech
  recognition when available).
- Replies are read aloud with a **server-side neural TTS** tuned for a British
  butler cadence (toggle with the "Voice" chip in the header). This works in the
  web UI and the Tauri desktop app — it does **not** depend on OS / browser speech
  synthesis.
- Default voice: `en-GB-ThomasNeural` via [edge-tts](https://github.com/rany2/edge-tts)
  (slightly slower and lower-pitched). Override with env vars or Settings-backed
  config:

```bash
export JARVIS_TTS_VOICE="en-GB-ThomasNeural"   # or en-GB-RyanNeural
export JARVIS_TTS_RATE="-12%"
export JARVIS_TTS_PITCH="-6Hz"
```

- Audio is cached under `~/.jarvis/tts_cache/`. The backend needs outbound network
  access to Microsoft’s Edge TTS endpoint for synthesis.
- **Push-to-talk** records with `MediaRecorder` and transcribes locally via
  [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (`POST /api/stt`).
  Free and offline after the model downloads once (default `base.en`). Requires
  `ffmpeg` on PATH. Override with:

```bash
export JARVIS_STT_MODEL="base.en"       # or tiny.en / small.en / …
export JARVIS_STT_DEVICE="cpu"          # or cuda
export JARVIS_STT_COMPUTE_TYPE="int8"   # or float16 on GPU
```

  Hold the mic button or **Super+`** to dictate.

### Device learning

JARVIS profiles the host automatically on startup (hardware, desktop, control utilities):

```bash
export JARVIS_DEVICE_AUTO_LEARN=true    # default true
export JARVIS_DEVICE_REFRESH_HOURS=6    # periodic re-scan
```



## Telegram (remote chat)

Talk to JARVIS on your laptop from Telegram without opening any ports — the backend
long-polls Telegram outbound.

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.
2. Enable the bot (env or Settings → Telegram):

```bash
export JARVIS_TELEGRAM_ENABLED=true
export JARVIS_TELEGRAM_BOT_TOKEN="123456:ABC…"
```

3. Restart JARVIS (`uv run jarvis`), open a DM with your bot, send `/whoami`.
4. Add the printed `chat_id` (or `user_id`) to the allowlist:

```bash
export JARVIS_TELEGRAM_ALLOWED_CHAT_IDS="123456789"
```

   Or paste them in **Settings → Telegram** and Save (the bot reloads automatically).

5. Chat normally. Commands: `/new`, `/cancel`, `/status`, `/help`. Photos with captions
   work as vision attachments. Destructive tools send **Approve / Deny** buttons in Telegram
   (unless auto-approve is enabled).

An empty allowlist rejects everyone — required for safety since the bot can control this machine.



## Global hotkey (desktop)

The Tauri desktop app registers a global shortcut (default `Ctrl+Alt+J`, configurable in
Settings or via `JARVIS_HOTKEY`) that shows/hides the JARVIS window from anywhere.

### Push-to-talk

Hold **Super+`** anywhere to dictate a prompt (release to send). Registered as
`JARVIS_PTT_HOTKEY` (default `Super+Backquote`). Audio is transcribed locally with
faster-whisper. Restart the desktop app after changing hotkey env vars.

## How it works

```
You ──▶ Web/Desktop UI ──WebSocket──▶ Agent loop ──▶ LLM (Odysseus local / cloud fallback)
 │                                       │
 └──▶ Telegram bot (long-poll) ──────────┤
                                         ├──▶ Tools: device_control, browse, communicate, remember, tasks
                                         └──▶ Memory: SQLite + autonomous device profiles + recall
```

On startup JARVIS autonomously profiles the host (CPU, RAM, disks, desktop environment, package managers, audio/display/network utilities) and injects control hints into every conversation. Ask it to dim the screen, connect Wi-Fi, install a package, or open an app — it picks the right shell command from what it already knows. Use `remember` to persist newly discovered control methods.

The agent streams every step (reasoning, tool calls, results) to the Activity Monitor.
Chat and activity are saved per session (delete a session or Clear the monitor independently).
After configurable idle time, REM sleep runs light → REM → deep consolidation into
`~/.jarvis/MEMORY.md` and `DREAMS.md` (toggle / Run now in Settings).
Destructive tool calls pause and surface an approval dialog before running.

## Safety model

- Read-only / low-risk actions (search, read a file, list a directory, scan the system)
run automatically.
- Destructive / high-impact actions (delete, overwrite, moving files, arbitrary shell
writes, sending email) pause and require **Approve** in the UI before running.
- Everything JARVIS does is streamed to the Activity panel so you can watch it work.

