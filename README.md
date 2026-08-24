# JARVIS

A self-hosted, butler-style AI agent inspired by JARVIS from Iron Man.

- **Local-first LLM** via [Odysseus](https://odysseus-dev.github.io/odysseus/) (any OpenAI-compatible endpoint), with a cloud API fallback.
- **Full device control**: JARVIS learns your machine autonomously on startup and publishes an exhaustive catalogue of every action it can actually perform here — brightness, volume, Wi-Fi, Bluetooth, power, displays, media, services, packages, windows, clipboard, sensors — each already resolved to the right command for your OS, session, and installed tools. "Dim my screen" runs; it never comes back as a command for you to paste.
- **Application catalogue**: every installed app is inventoried — native packages, Flatpaks, Snaps, Wine titles, macOS bundles — along with *what each one is for*, so "create a Roblox Studio project" opens Vinegar and "edit this photo" opens GIMP without you naming the program. If nothing installed fits, JARVIS finds the package and installs it.
- **Self-provisioning MCP**: when a request needs an integration JARVIS doesn't have, it searches the public registries and installs one itself — remote Streamable HTTP, or **locally over stdio** via `npx`/`uvx`/`docker` — then calls the imported tools in the same turn.
- **Computer use (GUI)**: screenshots + mouse/keyboard so JARVIS can complete arbitrary desktop tasks the way you would — click, type, scroll, switch windows — on Linux (Wayland/X11), macOS, or Windows.
- **Peripherals**: detects USB, Bluetooth, audio, displays, cameras, printers, storage, Wi-Fi and LAN/mDNS neighbours; can inspect/learn them, connect or pair, and control volume, brightness, mount, and media.
- **Full internet access**: web search + page reading so it can learn to answer your prompts.
- **Persistent memory**: chat sessions, activity monitor history, and device profiles survive restarts; REM sleep consolidates short-term memories when idle.
- **Neon-tech web UI** streaming everything JARVIS is thinking and doing in real time.
- **Voice** interaction (local faster-whisper STT; butler-style neural TTS from the backend).
- **Desktop app** (Tauri): starts the backend, stays in the tray when closed,
  summons with a global hotkey, and **push-to-talk** (hold Super+\`) with a
  listening orb overlay. Visible on Waybar via tray + optional custom module.
- **Telegram bot** for remote chat from your phone (outbound long-poll; laptop stays private).

> JARVIS can run shell commands, drive the GUI (mouse/keyboard/screenshots),
> modify files, and send email on your behalf.
> Irreversible actions (delete/overwrite, shutdown/reboot, send email) require
> explicit confirmation in the UI. The backend binds to `127.0.0.1` only.
> Use at your own risk on a machine you control.



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



### 3. Desktop app

The desktop app starts the backend, the UI, hotkeys, and a tray icon by itself.
Closing the window leaves JARVIS running in the background (Waybar tray + Super+\`
still work). Quit from the tray to stop everything.

On **Linux**, WebKitGTK needs GStreamer for microphone capture and TTS playback.
Install the plugins once (Arch example — you already have `gstreamer` and
`gst-plugins-base`; **`gst-plugins-good` is the usual missing piece**):

```bash
sudo pacman -S gst-plugins-good pipewire-pulse
```

Debian/Ubuntu: `sudo apt install gstreamer1.0-plugins-good`

```bash
cd desktop
npm install
npm run tauri dev
```

`npm run dev` checks for the required GStreamer elements before launching.

The first launch enables **login autostart** (XDG) so JARVIS comes up in the
background after you log in. A `jarvis` symlink is created at `~/.local/bin/jarvis`.

- **Ctrl+Alt+J** — summon / dismiss the console (`JARVIS_HOTKEY`).
- **Hold Super+`** — push-to-talk from anywhere. A listening orb overlay appears
  while you hold; release to send (`JARVIS_PTT_HOTKEY`, default `Super+Backquote`).
- **Close the window** — hide to tray; the server keeps running.
- **Tray → Quit JARVIS** — stop the desktop shell and the backend it started.

#### Computer use (GUI automation)

JARVIS controls **the machine it is installed on**. Install it on any Linux,
macOS, or Windows host you want it to operate. For on-screen tasks it uses
`computer_use` (screenshot → click/type/scroll).

**Linux Wayland** (Hyprland / Sway / GNOME Wayland):

```bash
# Arch
sudo pacman -S grim wl-clipboard
# ydotool: AUR or build from source; then:
systemctl --user enable --now ydotoold
# ensure your user can write /dev/uinput (often: input group)
```

**Linux X11**:

```bash
sudo pacman -S maim xdotool xclip   # or: apt install maim xdotool xclip
```

**macOS**: `screencapture` is built-in; `brew install cliclick` for precise mouse.
Grant Accessibility to the terminal / JARVIS process in System Settings.

**Windows**: no extra packages — PowerShell/.NET handles capture and input.

Ask JARVIS something like “open Firefox and search for weather” — it should
screenshot, click, and type rather than only printing shell commands.

#### Waybar

Enable Waybar's `tray` module to see the JARVIS icon while the console is closed.

Optional custom module (click to open the console):

```jsonc
"custom/jarvis": {
  "exec": "$HOME/.jarvis/waybar.sh",
  "interval": 3,
  "return-type": "json",
  "on-click": "jarvis"
}
```

If the tray icon does not appear, install a StatusNotifier tray library
(`libappindicator-gtk3` on Arch) and add `"tray"` to Waybar `modules-right`.

## Connecting to Odysseus (local model)

1. Install and run [Odysseus](https://odysseus-dev.github.io/odysseus/) and serve a model
  (via its Cookbook / Ollama / vLLM / llama.cpp).
2. Point JARVIS at the OpenAI-compatible endpoint it exposes, e.g.
  `http://localhost:11434/v1`, and set the model name to the one you're serving.
3. Optionally set a cloud fallback (OpenAI / OpenRouter / Anthropic-compatible) so JARVIS
  keeps working if the local endpoint is down.



## Voice

- Speak to JARVIS with the microphone button in the composer (hold to talk, release to
  send) or the desktop **push-to-talk** hotkey. You can barge in **while JARVIS is
  still working**: your next utterance is folded into the current task rather than
  aborting it.
- JARVIS **talks while it works** — short spoken status lines as it plans and uses
  tools, then a concise final reply. Follow up on any of those lines, elaborate the
  original request, or cancel entirely.
- **Cancel**: press **Stop** / **Esc**, or say “stop” / “cancel” / “never mind”.
  That ends the current run. Anything else you send while it is busy is a live
  follow-up.
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

JARVIS profiles the host automatically on startup (hardware, desktop, control utilities)
and inventories attached / nearby peripherals:

```bash
export JARVIS_DEVICE_AUTO_LEARN=true    # default true
export JARVIS_DEVICE_REFRESH_HOURS=6    # periodic host re-scan
export JARVIS_SCAN_PERIPHERALS=true     # USB, Bluetooth, audio, displays, …
export JARVIS_PERIPHERAL_REFRESH_MINUTES=15
```

Ask it to list headphones, connect a Bluetooth device, set a speaker as default, or
inspect a USB camera. The **Peripherals** chip in the header shows the live inventory;
**Discover** runs a Bluetooth inquiry for unpaired nearby devices.

"Connect my headphones" is a single call: `peripherals action=connect` discovers the
device if it is not in the inventory yet, pairs and trusts it if it never was, connects
it, and routes audio to it when it is a headset — no scan/pair/pick-a-sink sequence.

### Device controls

Learning the machine is not the same as knowing what to *do* with it. On every scan
JARVIS probes the host against a catalogue of device actions and keeps the ones this
machine can genuinely perform — one verified command per action, picking `wpctl` over
`pactl` over `amixer`, `hyprctl` over `swaymsg` over `xrandr`, `pacman` over `apt`,
according to what is actually installed:

```
device_control action=control  control=<id>  value=<v>
device_control action=controls query=<word>          # search what this host can do
```

```
display.brightness.set value=30        audio.volume.down value=10
network.wifi.connect value=<ssid>      bluetooth.power.on
power.profile.set value=performance    media.next
session.workspace.switch value=3       packages.install value=<pkg>
```

A typical Arch/Hyprland laptop yields around 200 actions across Display, Audio, Media,
Network, Bluetooth, Power, Services, System, Storage, Packages, Session, Input,
Lighting, Peripherals and Containers. The catalogue is in the system prompt, so the
model looks up an id instead of recalling shell syntax, and the value is validated
before anything runs.

Two things follow from this:

**Direct requests execute directly.** "Dim my screen", "turn the volume down", "mute",
"lock the screen" resolve to a catalogued action and run without a model round-trip —
the wording is unambiguous and there is exactly one sensible execution. Questions
("how do I dim my screen"), peripheral-scoped asks ("dim my *keyboard*"), and anything
risky or privileged still go to the model.

**A pasted command is treated as a mistake.** If a reply consists of a shell command
rather than a report of work done, JARVIS runs it through the normal tool path —
confirmation gates included — instead of handing it back to you.

Actions this host is missing are listed too, with the package that unlocks them
(`openrgb` → RGB lighting, `cups` → printing, `power-profiles-daemon` → power
profiles), so JARVIS installs the package and completes the request rather than
reporting the capability as unavailable.

### Application catalogue

Alongside the host profile, JARVIS inventories every launchable application from XDG
desktop entries, `flatpak list`, `snap list`, macOS `.app` bundles and the Windows Start
Menu, and records what each one is *for*. That inventory goes into the system prompt, so
JARVIS picks the right program without being told which:

| You say | JARVIS opens |
| --- | --- |
| "create a new Roblox Studio project" | Vinegar (`org.vinegarhq.Vinegar`) |
| "play Roblox" | Sober (`org.vinegarhq.Sober`) |
| "edit this photo" | GIMP |
| "make me a logo" | Inkscape |
| "record my screen" | OBS Studio |

```
device_control action=open  app=<name | app id | purpose>  [file=<document>]
device_control action=apps  query=<name or purpose>
```

`action=open` accepts a name (`krita`), an app id (`org.kde.krita`), or a purpose
(`"vector graphics"`), and launches Flatpaks and Snaps correctly rather than assuming the
program is on `PATH`. `action=apps` searches the catalogue and, when nothing installed
fits, returns install commands for your package manager so JARVIS can provision the app
itself and carry on.



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
   work as vision attachments. You can keep messaging while JARVIS is working — that
   elaborates or follows up; `/cancel` (or “stop”) aborts. Irreversible tools send
   **Approve / Deny** buttons in Telegram (unless auto-approve is enabled).

An empty allowlist rejects everyone — required for safety since the bot can control this machine.



## MCP (Model Context Protocol)

JARVIS is both an MCP **server** (expose its tools to other clients) and an MCP
**client** (import other servers' tools for itself).

### JARVIS extends itself

When a request needs a capability JARVIS lacks, it provisions one without being asked:

```
mcp_discover mode=ensure goal="roblox studio automation"
```

That searches the public registries under several phrasings, ranks candidates by
relevance and by how likely they are to connect unattended, installs the best one, and
reports the tool names it can now call. Servers are installed either as **remote**
Streamable HTTP endpoints or, when a server publishes no URL (the common case), as
**local stdio subprocesses** launched via `npx`, `uvx`, or `docker`.

Safeguards, because unattended installs deserve them:

- A candidate must mention a distinctive word from the goal — "control spotify playback"
  will not install an export-control server just because it matched "control".
- A server that fails to connect is rolled back, so config never accumulates dead entries.
- Candidates gated behind an API key are reported by name instead of half-installed, so
  JARVIS can ask you for that one key: `mode=install catalog_id=<id> authorization=<key>`.

Other modes: `search` (browse candidates), `install` (a specific `catalog_id`, or a local
server via `command`/`args`/`env`), `list` (installed servers and their imported tools),
`refresh`, `remove`.

### Serving other clients

Connect Cursor, Claude Desktop, or any MCP client to JARVIS over **Streamable HTTP**
(stateless; no port exposure beyond the local API). Use the **MCP** chip in the web UI to
browse public directories (official registry, Smithery, MCP.so) and one-click install
servers — locally-run servers appear there marked `local`.

1. Start JARVIS (`uv run jarvis`).
2. Point your MCP client at:

```
http://127.0.0.1:8787/mcp
```

**Cursor** (`~/.cursor/mcp.json` or project MCP settings):

```json
{
  "mcpServers": {
    "jarvis": {
      "url": "http://127.0.0.1:8787/mcp"
    }
  }
}
```

**What you get**

- All JARVIS agent tools (`device_control`, `browse`, `peripherals`, `remember`, task tools, …)
- `jarvis_ask` — run the full agent loop on a natural-language request
- Read-only resources: long-term memory (`jarvis://memory`), device profile, peripherals, tool catalog, conversation history
- Prompt template: `ask_jarvis`

Irreversible tool calls are **denied over MCP** unless auto-approve is enabled
(`JARVIS_AUTO_APPROVE=true` or Settings → Permissions). Use the web UI for interactive
Approve/Deny, or enable auto-approve only on a machine you fully trust.

Disable the endpoint with `JARVIS_MCP_ENABLED=false`.



## Global hotkey (desktop)

**Ctrl+Alt+J** (configurable in Systems or `JARVIS_HOTKEY`) summons or dismisses the
console. Closing the window hides it; JARVIS stays in the tray.

### Push-to-talk

Hold **Super+`** anywhere. The listening orb overlay appears while you hold; release
to send. Works with the console closed. Override with `JARVIS_PTT_HOTKEY`
(default `Super+Backquote`). Restart the desktop app after changing hotkey env vars.

On Hyprland, if the overlay sits behind other windows, add:

```
windowrulev2 = float, title:^(JARVIS Listening)$
windowrulev2 = pin, title:^(JARVIS Listening)$
```

## How it works

```
You ──▶ Web/Desktop UI ──WebSocket──▶ Agent loop ──▶ LLM (Odysseus local / cloud fallback)
 │                                       │
 ├──▶ Telegram bot (long-poll) ──────────┤
 ├──▶ MCP clients (Streamable HTTP) ─────┤
                                         ├──▶ Tools: device_control, peripherals, browse, communicate, remember, tasks
                                         └──▶ Memory: SQLite + autonomous device/peripheral profiles + recall
```

On startup JARVIS autonomously profiles the host (CPU, RAM, disks, desktop environment, package managers, audio/display/network utilities) and scans for peripherals (USB, Bluetooth, audio sinks, displays, cameras, printers, storage, Wi-Fi, mDNS). Control hints are injected into every conversation. Ask it to dim the screen, connect headphones, join Wi-Fi, install a package, or open an app — it picks the right tool from what it already knows. Use `remember` (or `peripherals remember`) to persist newly discovered control methods.

The agent streams every step (reasoning, tool calls, results) to the Activity Monitor.
Chat and activity are saved per session (delete a session or Clear the monitor independently).
After configurable idle time, REM sleep runs light → REM → deep consolidation into
`~/.jarvis/MEMORY.md` and `DREAMS.md` (toggle / Run now in Settings).
Irreversible tool calls pause and surface an approval dialog before running.

## Safety model

- Most actions (search, read, install, volume, lighting, open apps, pairing, sudo
with a saved password) run immediately.
- Irreversible actions (delete or overwrite files, move, shutdown/reboot, sending
email) pause and require **Approve** in the UI before running.
- Missing configuration (no sudo password, email not set up) is reported with an
optional next-step suggestion — not an Approve dialog.
- Auto-approve (Settings → Authorisation) skips even the irreversible pauses.
- Everything JARVIS does is streamed to the Activity panel so you can watch it work.

