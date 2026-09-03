# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

JARVIS is a self-hosted butler-style AI agent that controls the machine it runs on: a
FastAPI backend (`backend/`) with the agent loop, tools and memory; a React/Vite UI
(`web/`); and a Tauri shell (`desktop/`) that launches the backend, a tray icon, a global
hotkey and push-to-talk.

## Commands

```bash
# Backend
cd backend
uv sync                       # or: pip install -e .
uv run jarvis                 # API + WebSocket on http://127.0.0.1:8787

# Tests (stdlib unittest; no test dependency is installed)
cd backend && ./.venv/bin/python -m unittest discover -s tests
cd backend && ./.venv/bin/python -m unittest tests.test_agent_loop.ThinLoopTests            # one class
cd backend && ./.venv/bin/python -m unittest tests.test_agent_loop.ThinLoopTests.test_tool_call_then_text

# Web UI
cd web && npm install && npm run dev      # http://localhost:5173
cd web && npm run build                   # tsc && vite build — the only type check in the repo

# Desktop (starts the backend itself)
cd desktop && npm install && npm run tauri dev
```

There is no linter or formatter configured. `npm run build` in `web/` is the type check.

### Running backend code by hand — set `JARVIS_DATA_DIR` first

Importing `jarvis.config` creates and reads `~/.jarvis` (config, SQLite DB, MEMORY.md),
so any ad-hoc script, REPL or test that imports the package will read and *overwrite the
user's live configuration* unless redirected:

```bash
JARVIS_DATA_DIR=/tmp/jarvis-scratch ./.venv/bin/python -c "import jarvis.main"
```

`backend/tests/` sets it before importing. Never skip this.

## Architecture

### Turn lifecycle

`web` opens one WebSocket to `/ws/chat` and sends `{type: "user_message", conversation_id, text}`.
`main.py` owns a registry of live runs keyed by asyncio task and dispatches to
`Agent.run` (`agent.py`), which:

1. stores the user turn in SQLite and ingests any attachments (`media.py`),
2. runs a tool-free multimodal **vision scan** first when media is attached, so the
   model has looked at the pixels before the tool loop (many backends drop images from
   tool-call requests),
3. builds messages via `_build_messages` — persona + injected machine context + this
   conversation's history,
4. runs `_tool_loop`, streaming `thought_start` / `thought_delta` / `tool_call` /
   `tool_result` / `say` / `assistant` events back over the socket. The UI renders these
   as the Activity Monitor and PUTs the feed back to
   `/api/conversations/{id}/activity` so it survives a reload.

`confirmation_request` → the UI's Approve dialog → a `confirm` WS message resolves the
future awaited inside `Agent._execute_tool`.

### The agent loop is a thin executor — keep it that way

`_tool_loop` sends messages + tool schemas, runs whatever tool calls come back, appends
`role=tool` results, and repeats until the model replies in plain text. That is the whole
contract. Judgement about *what* to do belongs to the model and to `persona.py`.

Deliberately **not** in the loop (an earlier version had all of these and they were
removed): guards that append `[system]` nudges and `continue`, forced `tool_choice`,
intent classifiers, fast paths that answer before the model sees the turn, and
synthesized replies that replace the model's text. Do not reintroduce agent-side pattern
matching to fix a bad reply — improve the tool schema/description or the system prompt.

Two opt-in crutches for weak local models live in `config.agent`, both default-off:

- `strict_tools` (`"auto"` | `"on"` | `"off"`, default `auto`) — recover a tool call the
  model narrated as prose, and run a command it pasted as its reply, both through the
  normal approval gates. `auto` resolves via `AgentConfig.recover_narrated_calls()` to on
  for a local endpoint (`LLMConfig.is_local`) and off for a cloud one. This is not
  optional polish for local models: qwen3.5 through Ollama picks the right tool and
  arguments and then writes them as chat text at any realistic prompt size — it only
  emits structured calls when the system prompt is trivially short. All the parsing lives
  below the `Strict-tools recovery` banner in `agent.py`, and the argument names come from
  the live tool schemas (`_tool_param_keys`), because a hardcoded list silently dropped
  `control=…` — the one argument that mattered. `RecoveryParsingTests` pins real
  transcripts.
- `trust_model=false` — one retry when a reply comes back completely empty.

Kept unconditionally because they are not judgement about the request: barge-in/cancel
(`RunControl`), dangerous-tool confirmation + `auto_approve`, unknown-tool errors, event
streaming, and the iteration budget with a tool-free `_wrap_up`.

**Barge-in vs cancel:** while a run is live, a new user message on the same conversation
is *injected* (`RunControl.inject`) rather than starting a new run. The LLM stream aborts,
`AgentSteered` unwinds to a checkpoint, unstarted tool calls are closed out with a
"not run" result, and the utterance is folded into the same turn. `is_cancel_utterance`
("stop", "never mind") turns a barge-in into `AgentInterrupted`.

### A few universal tools, not one tool per action

`tools/__init__.py` registers 13 tools; `all_tools()` appends MCP proxy tools discovered
at runtime, so the schema list is dynamic. The tool surface is intentionally coarse —
each takes an `action` discriminator:

- **`device_control`** — `control`, `controls`, `shell`, `read`, `write`, `list`, `move`,
  `delete`, `open`, `apps`. `action=control control=<id>` is the primary path.
- **`peripherals`** — `list`, `scan`, `inspect`, `connect`, `disconnect`, `pair`,
  `unpair`, `control`, `remember`.
- **`computer_use`** — screenshot/click/type/key/scroll/drag/focus. Returns
  `ToolResult.images`; the loop injects those as a multimodal *user* turn because
  `role=tool` messages cannot carry images on most OpenAI-compatible endpoints.
- `browse`, `communicate` (email), `remember`, `mcp_discover`, `mcp_invoke`, and the
  task tools (`start_task`, `run_background_shell`, `check_task`, `list_tasks`,
  `cancel_task`).

A tool declares `dangerous` (bool or predicate over args) in its `Tool` dataclass;
`Agent._execute_tool` is the single gate that turns that into a confirmation. Risk copy
comes from `_hard_gate_risk`.

### Learned machine context (the reason it can act without being told how)

`_build_messages` injects two context blocks into every system prompt:
`DeviceLearner.get_context()` and `PeripheralLearner.get_context()`. The first is
assembled by `device.format_device_context()`, which stitches together three sources —
follow that function to see what the model actually knows.

**These are indexes, not listings, and that is load-bearing.** Each source has a
`format_*_index` (compact) and a `format_*_context` / `format_inventory` (exhaustive)
form; `config.agent.full_device_context` picks between them and defaults to the index.
The full dump is ~6k tokens before the user's message and demonstrably hijacks short
requests — asked for a news briefing, qwen3.5 answered with a hardware status report and
made no tool call. Every index therefore ends by naming the tool call that retrieves the
detail (`action=controls query=…`, `action=apps query=…`, `peripherals action=list`). If
you add to any of these blocks, add it to the exhaustive form, not the index.


- **`device.py`** — the host profile it scans (CPU, RAM, disks, desktop/session, init
  system, package managers, installed tools) and control hints.
- **`controls.py`** — a hand-written catalogue of `Control` records (id, category,
  command template, platform/session/binary requirements, `reads`/`risky`/`sudo` flags).
  `available()` filters to what this host actually supports (Wayland vs X11, which
  binaries exist) and `format_controls_context()` renders the survivors, while
  `installable()` lists controls a package install would unlock. This is why the model
  never composes `brightnessctl` syntax — it names a control id and `device_control`
  resolves the command for this host.
- **`appcatalog.py`** — every installed app (desktop entries, Flatpak, Snap, macOS
  bundles, Windows shortcuts, CLI tools) with a fuzzy `resolve()` over name *and purpose*,
  so "roblox studio" finds Vinegar.

**`peripherals.py`** is the second block: USB/Bluetooth/audio/display/camera/printer/
storage/Wi-Fi/mDNS inventory plus the connect/pair/control implementations behind the
`peripherals` tool.

Both learners run on startup and refresh periodically (`config.device`), broadcasting
progress to all WS clients.

### Memory model (`memory.py`) — durable vs session-scoped

One SQLite DB (`~/.jarvis/jarvis.db`): `conversations`, `messages`, `activity_items`,
`devices`, `peripherals`, `tasks`, and `memories` (kind, text, embedding).

`memories` rows split in two, and the distinction is load-bearing:

- **Episodic** — `conversation`, `staged`, `short_term` (`_EPISODIC_KINDS`). Session
  scoped. `recall()` **excludes them by default**; only pass `include_episodic=True` from
  REM staging. Injecting them into a new chat used to make JARVIS resume a task from a
  conversation that had ended.
- **Durable** — `preference`, `long_term`, `device`, `device_control`, `peripheral`,
  `rem_theme`, `application`. These cross sessions, and arrive in the prompt under a
  "Background notes" preamble that says explicitly they are facts, never a task.

Per-turn summaries are not stored as recallable memories unless
`config.agent.store_conversation_memories` is on. The `remember` tool is the durable path
and its description exists to keep task briefs out of it.

`memory.is_self_belief()` filters a third category out of recall entirely, whatever kind
it was stored under: notes about what JARVIS itself can or cannot do. REM had promoted
"…despite browsing limitations" to `long_term`, where it was recalled into later turns
and taught the model that the limitation was real — which produced the next hedge, and
the next note. The filter is intentionally narrow (an unmistakable tooling phrase, or a
capability denial whose subject is the assistant) so that real facts like "the printer
has no network access" survive; `SelfBeliefTests` pins both directions.

**`rem.py`** — after idle time (`config.rem`), a light → REM → deep sweep stages recent
signals, extracts themes via the LLM, and promotes durable ones into `long_term` plus
`~/.jarvis/MEMORY.md` / `DREAMS.md`. Its prompt and `_is_ephemeral_memory_line` both
exist to stop task-shaped lines being promoted into permanent memory.

### MCP goes both ways

- **Server** (`mcp_server.py`): mounted at `/mcp`; exposes `jarvis_ask`, every tool, and
  `jarvis://` resources (memory, dreams, device profile, peripherals, conversations).
- **Client** (`mcp_client.py`): configured servers are connected and their tools imported
  as `mcp_<server>_<tool>` proxy `Tool`s, plus `format_mcp_context()` for the prompt.
  Transports are **http or stdio** — a stdio entry carries `command`/`args`/`env`/`cwd`
  instead of a URL.
- **Self-provisioning**: `mcp_discover mode=ensure goal=…` searches public registries
  (`mcp_catalog.py`), installs and connects a server, and returns its tool names, so the
  model can give itself a missing capability mid-turn.

### Config layering (`config.py`)

Env var defaults → `~/.jarvis/config.json` → Settings UI. `ConfigStore._apply` **merges
per key**: clients POST partial payloads to `/api/config` (`{data: {...}}`), and absent
keys keep their stored values. This matters most for MCP entries — a client that only
knows about remote servers omits `command`/`args`/`env`, and `_apply_mcp` deliberately
carries those through rather than unconfiguring a working stdio server. Masked secrets
arrive as `********` and are preserved the same way.

`Agent` flags live under `config.agent` (`trust_model`, `strict_tools`, `max_iterations`,
`store_conversation_memories`) and have no Settings UI — env vars or `config.json` only.

### Background tasks (`tasks.py`)

`TaskManager` owns subagent runs and detached shells so they survive chat interrupts and
disconnects. A subagent is a fresh `Agent` at `depth=1` running `Agent.run_task` with a
synthetic context (no conversation history) and `SUBAGENT_ADDENDUM` appended to the
persona: no user is present, so confirmations auto-deny unless `auto_approve` is set, and
subagents may not spawn subagents. State persists in the `tasks` table; progress
broadcasts to every WS client.

### Persona (`persona.py`)

One `PERSONA` template plus privilege/approval notes computed from
`config.permissions` (whether a sudo password is saved, whether `auto_approve` is on), with
the learned machine context appended. It is organised around goals — infer intent, choose
the means, act, report — not around a list of past mistakes. Two rules to preserve when
editing: a command belongs in a tool argument and never in a chat or spoken reply, and the
model may end with up to three `[[suggest: label | prompt]]` trailers, which
`_extract_suggestions` strips into UI chips.

Two last things worth knowing: the backend binds `config.host` (default `127.0.0.1`),
and `llm.py` is local-first with a cloud fallback chosen by `config.llm.prefer`, where
embeddings are optional — `recall()` falls back to keyword scoring when no embedding
endpoint is configured.
