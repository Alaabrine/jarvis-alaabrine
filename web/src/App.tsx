import { useCallback, useEffect, useRef, useState } from "react";
import { api, wsUrl } from "./api";
import { useVoice } from "./voice";
import { usePushToTalkHotkey } from "./usePushToTalkHotkey";
import type {
  ActivityItem,
  AgentState,
  BackgroundTask,
  ChatMessage,
  ConfirmationRequest,
  Conversation,
  McpStatus,
  Peripheral,
  RemLogEntry,
  RemStatus,
} from "./types";
import { Sidebar } from "./components/Sidebar";
import { ChatPanel } from "./components/ChatPanel";
import { ActivityFeed } from "./components/ActivityFeed";
import { ConfirmModal } from "./components/ConfirmModal";
import { SettingsModal, type SystemsSection } from "./components/SettingsModal";
import { StatusOrb } from "./components/StatusOrb";
import { RemSleepView } from "./components/RemSleepView";
import { TasksView } from "./components/TasksView";
import { PeripheralsView } from "./components/PeripheralsView";
import { McpView } from "./components/McpView";
import { MemoryView } from "./components/MemoryView";
import { HudOverlay } from "./components/HudOverlay";
import { BOOT_KEY, SystemsBoot } from "./components/SystemsBoot";
import type { BriefingStatus } from "./components/WelcomeBriefing";

let activitySeq = 0;
const nextId = () => `a${Date.now()}-${activitySeq++}`;

export default function App() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [currentId, setCurrentId] = useState<number | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [activity, setActivity] = useState<ActivityItem[]>([]);
  const [confirmations, setConfirmations] = useState<ConfirmationRequest[]>([]);
  const [agentState, setAgentState] = useState<AgentState>("idle");
  const [online, setOnline] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsSection, setSettingsSection] = useState<SystemsSection>("overview");
  const [voiceReplies, setVoiceReplies] = useState(true);
  const [remStatus, setRemStatus] = useState<RemStatus | null>(null);
  const [remLogs, setRemLogs] = useState<RemLogEntry[]>([]);
  const [remViewOpen, setRemViewOpen] = useState(false);
  const [tasks, setTasks] = useState<BackgroundTask[]>([]);
  const [view, setView] = useState<"console" | "tasks" | "peripherals" | "mcp" | "memory">("console");
  const [memoryCount, setMemoryCount] = useState(0);
  const [peripherals, setPeripherals] = useState<Peripheral[]>([]);
  const [mcpStatus, setMcpStatus] = useState<McpStatus | null>(null);
  const [periScanning, setPeriScanning] = useState(false);
  const [periError, setPeriError] = useState("");
  const [briefing, setBriefing] = useState<BriefingStatus | null>(null);
  const [bootOpen, setBootOpen] = useState(false);

  const wsRef = useRef<WebSocket | null>(null);
  const currentIdRef = useRef<number | null>(null);
  const activityRef = useRef<ActivityItem[]>([]);
  const tasksRef = useRef<BackgroundTask[]>([]);
  const skipSaveRef = useRef(false);
  currentIdRef.current = currentId;
  activityRef.current = activity;
  tasksRef.current = tasks;

  const handleTranscript = useCallback((text: string) => {
    void sendMessage(text, []);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const voice = useVoice(handleTranscript);
  const voiceRef = useRef(voice);
  voiceRef.current = voice;

  usePushToTalkHotkey({
    start: useCallback(() => voiceRef.current.startPushToTalk(), []),
    stop: useCallback(() => voiceRef.current.stopPushToTalk(), []),
  });

  function refreshBriefing() {
    api
      .getConfig()
      .then((c) => {
        const mail = c.email_accounts?.profiles?.find((p) => p.id === c.email_accounts?.default_id) || c.email;
        const emailReady = Boolean(mail?.smtp_host && (mail?.from_address || mail?.smtp_user));
        const telegramReady = Boolean(
          c.telegram?.enabled && (c.telegram?.bot_token_configured || (c.telegram?.bot_token && c.telegram.bot_token !== "********"))
        );
        setBriefing({
          userName: c.user_name || "Sir",
          model: c.llm?.prefer === "cloud" ? c.llm.fallback_model || c.llm.model : c.llm?.model || "",
          prefer: c.llm?.prefer || "local",
          telegramReady,
          emailReady,
          peripheralCount: peripherals.length,
          connectedPeripherals: peripherals.filter((p) => p.connected).length,
          mcpTools: mcpStatus?.imported_tool_count ?? 0,
        });
      })
      .catch(() => {});
  }

  // --- Load conversations + rem status on mount ---
  useEffect(() => {
    void refreshConversations();
    api.health().then(() => setOnline(true)).catch(() => setOnline(false));
    api.remStatus().then(setRemStatus).catch(() => {});
    api.listTasks().then(setTasks).catch(() => {});
    api.listPeripherals().then(setPeripherals).catch(() => {});
    api.mcpStatus().then(setMcpStatus).catch(() => {});
    api.memoryStats().then((s) => setMemoryCount(s.total)).catch(() => {});
    refreshBriefing();
    try {
      if (localStorage.getItem(BOOT_KEY) !== "1") {
        api
          .listConversations()
          .then((list) => {
            if (list.length === 0) setBootOpen(true);
            else localStorage.setItem(BOOT_KEY, "1");
          })
          .catch(() => {});
      }
    } catch {
      /* ignore */
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    setBriefing((prev) =>
      prev
        ? {
            ...prev,
            peripheralCount: peripherals.length,
            connectedPeripherals: peripherals.filter((p) => p.connected).length,
            mcpTools: mcpStatus?.imported_tool_count ?? prev.mcpTools,
          }
        : prev
    );
  }, [peripherals, mcpStatus]);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key !== "Escape") return;
      if (confirmations.length > 0) return;
      if (bootOpen) return;
      if (settingsOpen) {
        setSettingsOpen(false);
        e.preventDefault();
        return;
      }
      if (remViewOpen) {
        setRemViewOpen(false);
        e.preventDefault();
        return;
      }
      if (view !== "console") {
        setView("console");
        e.preventDefault();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [confirmations.length, bootOpen, settingsOpen, remViewOpen, view]);

  // Persist activity monitor (debounced) per session
  useEffect(() => {
    const cid = currentId;
    if (cid === null || skipSaveRef.current) return;
    const timer = window.setTimeout(() => {
      api.saveActivity(cid, activityRef.current).catch(() => {});
    }, 700);
    return () => window.clearTimeout(timer);
  }, [activity, currentId]);

  function refreshMemoryCount() {
    api.memoryStats().then((s) => setMemoryCount(s.total)).catch(() => {});
  }

  async function refreshConversations() {
    try {
      const list = await api.listConversations();
      setConversations(list);
      if (list.length && currentIdRef.current === null) {
        void selectConversation(list[0].id);
      }
    } catch {
      /* backend offline */
    }
  }

  async function selectConversation(id: number) {
    setCurrentId(id);
    skipSaveRef.current = true;
    setActivity([]);
    try {
      const [msgs, acts] = await Promise.all([
        api.getMessages(id),
        api.getActivity(id),
      ]);
      setMessages(msgs);
      setActivity(Array.isArray(acts) ? acts : []);
    } catch {
      setMessages([]);
      setActivity([]);
    } finally {
      window.setTimeout(() => {
        skipSaveRef.current = false;
      }, 50);
    }
  }

  async function newConversation() {
    const { id } = await api.createConversation();
    await refreshConversations();
    setCurrentId(id);
    setMessages([]);
    skipSaveRef.current = true;
    setActivity([]);
    window.setTimeout(() => {
      skipSaveRef.current = false;
    }, 50);
  }

  async function removeConversation(id: number) {
    if (
      !window.confirm(
        "Delete this session? Chat history and activity monitor for it will be removed."
      )
    ) {
      return;
    }
    await api.deleteConversation(id);
    if (id === currentId) {
      setCurrentId(null);
      setMessages([]);
      skipSaveRef.current = true;
      setActivity([]);
      window.setTimeout(() => {
        skipSaveRef.current = false;
      }, 50);
    }
    await refreshConversations();
  }

  async function clearActivityMonitor() {
    const cid = currentIdRef.current;
    if (cid === null) {
      setActivity([]);
      return;
    }
    if (!window.confirm("Clear the activity monitor for this session?")) return;
    skipSaveRef.current = true;
    setActivity([]);
    try {
      await api.clearActivity(cid);
    } catch {
      /* ignore */
    } finally {
      window.setTimeout(() => {
        skipSaveRef.current = false;
      }, 50);
    }
  }

  // --- WebSocket ---
  useEffect(() => {
    connect();
    return () => wsRef.current?.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function connect() {
    const ws = new WebSocket(wsUrl());
    wsRef.current = ws;
    ws.onopen = () => {
      setOnline(true);
      // Re-sync task state after reconnects (events may have been missed).
      api.listTasks().then(setTasks).catch(() => {});
    };
    ws.onclose = () => {
      setOnline(false);
      setTimeout(connect, 2000);
    };
    ws.onmessage = (ev) => handleEvent(JSON.parse(ev.data));
  }

  function pushActivity(item: Omit<ActivityItem, "id" | "ts">) {
    setActivity((prev) => [...prev, { ...item, id: nextId(), ts: Date.now() }]);
  }

  function handleEvent(evt: any) {
    switch (evt.type) {
      case "conversation_created":
        setCurrentId(evt.id);
        void refreshConversations();
        break;
      case "agent_start":
        setAgentState("thinking");
        break;
      case "status":
        setAgentState(evt.state);
        if (evt.state === "awaiting_confirmation" || evt.state === "running_tool") {
          pushActivity({ kind: "status", state: evt.state });
        }
        break;
      case "thought_start":
        setActivity((prev) => [
          ...prev,
          {
            id: evt.id,
            ts: Date.now(),
            kind: "thought",
            reasoning: "",
            content: "",
            streaming: true,
          },
        ]);
        break;
      case "thought_delta":
        setActivity((prev) => {
          const copy = [...prev];
          const idx = copy.findIndex((a) => a.id === evt.id && a.kind === "thought");
          if (idx === -1) {
            copy.push({
              id: evt.id,
              ts: Date.now(),
              kind: "thought",
              reasoning: evt.channel === "reasoning" ? evt.text : "",
              content: evt.channel === "content" ? evt.text : "",
              streaming: true,
            });
            return copy;
          }
          const item = { ...copy[idx] };
          if (evt.channel === "reasoning") {
            item.reasoning = (item.reasoning || "") + evt.text;
          } else {
            item.content = (item.content || "") + evt.text;
          }
          copy[idx] = item;
          return copy;
        });
        break;
      case "thought_end":
        setActivity((prev) => {
          const copy = [...prev];
          const idx = copy.findIndex((a) => a.id === evt.id && a.kind === "thought");
          if (idx === -1) {
            const reasoning = evt.reasoning || "";
            const content = evt.content || "";
            if (!reasoning && !content) return prev;
            copy.push({
              id: evt.id,
              ts: Date.now(),
              kind: "thought",
              reasoning,
              content,
              streaming: false,
            });
            return copy;
          }
          const item = { ...copy[idx], streaming: false };
          if (evt.reasoning) item.reasoning = evt.reasoning;
          if (evt.content) item.content = evt.content;
          if (!(item.reasoning || "").trim() && !(item.content || "").trim()) {
            copy.splice(idx, 1);
            return copy;
          }
          copy[idx] = item;
          return copy;
        });
        break;
      case "assistant":
        setMessages((prev) => [...prev, { role: "assistant", content: evt.text }]);
        if (voiceReplies) voiceRef.current.speak(stripForSpeech(evt.text));
        break;
      case "suggestions": {
        const items = Array.isArray(evt.items) ? evt.items : [];
        if (!items.length) break;
        setMessages((prev) => {
          for (let i = prev.length - 1; i >= 0; i--) {
            if (prev[i].role === "assistant" && !prev[i].working) {
              const copy = [...prev];
              copy[i] = { ...copy[i], suggestions: items };
              return copy;
            }
          }
          return prev;
        });
        break;
      }
      case "say":
        if (evt.text) {
          setMessages((prev) => [
            ...prev,
            { role: "assistant", content: evt.text, working: true },
          ]);
          if (voiceReplies) voiceRef.current.speak(stripForSpeech(evt.text));
        }
        break;
      case "steered":
        pushActivity({
          kind: "status",
          state: "steered",
          text: evt.text || "",
        });
        break;
      case "tool_call":
        pushActivity({
          kind: "tool_call",
          name: evt.name,
          args: evt.args,
          dangerous: evt.dangerous,
          auto_approved: evt.auto_approved,
          output: undefined,
        });
        break;
      case "tool_result":
        setActivity((prev) => {
          for (let i = prev.length - 1; i >= 0; i--) {
            if (prev[i].kind === "tool_call" && prev[i].name === evt.name && prev[i].output === undefined) {
              const copy = [...prev];
              copy[i] = { ...copy[i], output: evt.output, ok: evt.ok };
              return copy;
            }
          }
          return [
            ...prev,
            {
              id: nextId(),
              ts: Date.now(),
              kind: "tool_result",
              name: evt.name,
              output: evt.output,
              ok: evt.ok,
            },
          ];
        });
        break;
      case "confirmation_request":
        setConfirmations((prev) => [
          ...prev,
          {
            id: evt.id,
            name: evt.name,
            args: evt.args,
            preview: evt.preview,
            risk: evt.risk,
          },
        ]);
        break;
      case "error":
        pushActivity({ kind: "error", text: evt.message });
        break;
      case "interrupted":
        setConfirmations([]);
        setAgentState("idle");
        setActivity((prev) => {
          const copy = prev.map((a) =>
            a.kind === "thought" && a.streaming ? { ...a, streaming: false } : a
          );
          return [
            ...copy,
            {
              id: nextId(),
              ts: Date.now(),
              kind: "status" as const,
              state: "interrupted",
            },
          ];
        });
        setMessages((prev) => {
          const stopLine = evt.message || "Very well — I'll stop there.";
          const last = prev[prev.length - 1];
          if (last?.role === "assistant" && last.content === stopLine) {
            return prev;
          }
          return [...prev, { role: "assistant", content: stopLine }];
        });
        voiceRef.current.stopSpeaking();
        break;
      case "agent_end":
        setAgentState("idle");
        setActivity((prev) =>
          prev.map((a) =>
            a.kind === "thought" && a.streaming ? { ...a, streaming: false } : a
          )
        );
        setMessages((prev) => {
          const last = prev[prev.length - 1];
          if (last?.role === "user") {
            return [
              ...prev,
              {
                role: "assistant",
                content:
                  "I finished that attempt without a visible reply. Please try again.",
              },
            ];
          }
          return prev;
        });
        void refreshConversations();
        break;
      case "rem_status": {
        const next: RemStatus = {
          enabled: evt.enabled,
          phase: evt.phase,
          running: evt.running,
          idle_seconds: evt.idle_seconds,
          idle_minutes_config: evt.idle_minutes_config,
          min_interval_minutes: evt.min_interval_minutes,
          last_activity_at: evt.last_activity_at,
          last_sweep_at: evt.last_sweep_at,
          last_result: evt.last_result,
          seconds_until_eligible: evt.seconds_until_eligible,
        };
        setRemStatus(next);
        if (next.running && next.phase === "dreaming") {
          setRemLogs([]);
          setRemViewOpen(true);
        } else if (next.running || next.phase !== "awake") {
          setRemViewOpen(true);
        }
        break;
      }
      case "rem_log":
        setRemLogs((prev) => [
          ...prev,
          {
            id: nextId(),
            phase: evt.phase || "dreaming",
            level: evt.level || "info",
            text: evt.text || "",
            ts: typeof evt.ts === "number" ? evt.ts * 1000 : Date.now(),
          },
        ]);
        setRemViewOpen(true);
        break;
      case "task_update": {
        const task: BackgroundTask | undefined = evt.task;
        if (!task) break;
        const previous = tasksRef.current.find((t) => t.id === task.id);
        const label = task.kind === "agent" ? "Subagent" : "Background process";
        if (!previous && task.status === "running") {
          pushActivity({
            kind: "task",
            name: task.id,
            text: `${label} deployed: ${task.title}`,
            state: "running",
          });
        } else if (previous?.status === "running" && task.status !== "running") {
          pushActivity({
            kind: "task",
            name: task.id,
            text: `${label} ${task.status}: ${task.title}`,
            state: task.status,
            output: task.result || undefined,
            ok: task.status === "completed",
          });
        }
        setTasks((prev) => {
          const idx = prev.findIndex((t) => t.id === task.id);
          if (idx === -1) return [task, ...prev];
          const copy = [...prev];
          copy[idx] = task;
          return copy;
        });
        break;
      }
      case "task_log":
        setTasks((prev) => {
          const idx = prev.findIndex((t) => t.id === evt.task_id);
          if (idx === -1) return prev;
          const copy = [...prev];
          const log = [...(copy[idx].log || []), ...(evt.entries || [])].slice(-200);
          copy[idx] = { ...copy[idx], log };
          return copy;
        });
        break;
      case "task_completed": {
        const task: BackgroundTask | undefined = evt.task;
        // The final report is persisted as an assistant message; pull it into the open chat.
        if (task?.conversation_id != null && task.conversation_id === currentIdRef.current) {
          api.getMessages(task.conversation_id).then(setMessages).catch(() => {});
        }
        if (task && voiceReplies) {
          voiceRef.current.speak(stripForSpeech(`Background task complete: ${task.title}.`));
        }
        break;
      }
      case "peripherals_updated": {
        api.listPeripherals().then(setPeripherals).catch(() => {});
        const n = typeof evt.count === "number" ? evt.count : 0;
        const c = typeof evt.connected === "number" ? evt.connected : 0;
        if (evt.reason && evt.reason !== "periodic") {
          pushActivity({
            kind: "peripheral",
            text: `Peripheral inventory: ${n} present, ${c} connected (${evt.reason})`,
            state: "updated",
          });
        }
        break;
      }
      case "peripheral_inspected":
        api.listPeripherals().then(setPeripherals).catch(() => {});
        pushActivity({
          kind: "peripheral",
          name: evt.id,
          text: `Learned about ${evt.name || evt.id}`,
          state: "learned",
        });
        break;
      case "peripheral_action":
        api.listPeripherals().then(setPeripherals).catch(() => {});
        pushActivity({
          kind: "peripheral",
          name: evt.id,
          text: `${evt.command || "action"} ${evt.name || evt.id}`,
          state: evt.ok ? "ok" : "fail",
          ok: Boolean(evt.ok),
        });
        break;
    }
  }

  async function sendMessage(
    text: string,
    attachments: import("./types").OutgoingAttachment[] = [],
    localMeta: import("./types").MediaAttachment[] = []
  ) {
    const trimmed = text.trim();
    if (!trimmed && attachments.length === 0) return;
    voiceRef.current.stopSpeaking();
    setRemViewOpen(false);
    setView("console");
    void api.remWake().then(setRemStatus).catch(() => {});
    let cid = currentIdRef.current;
    const isFirst = messages.length === 0;

    if (cid === null) {
      const created = await api.createConversation();
      cid = created.id;
      setCurrentId(cid);
      await refreshConversations();
    }

    setMessages((prev) => [
      ...prev,
      {
        role: "user",
        content: trimmed || (attachments.length ? "Please analyse the attached media." : ""),
        attachments: localMeta.length
          ? localMeta
          : attachments.map((a) => ({
              name: a.name,
              mime: a.mime,
              kind: a.mime.startsWith("video/") ? ("video" as const) : ("image" as const),
            })),
      },
    ]);

    if (isFirst && cid !== null) {
      const titleBase = trimmed || attachments.map((a) => a.name).join(", ") || "New session";
      const title = titleBase.slice(0, 42) + (titleBase.length > 42 ? "…" : "");
      api.renameConversation(cid, title).then(refreshConversations).catch(() => {});
    }

    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(
        JSON.stringify({
          type: "user_message",
          conversation_id: cid,
          text: trimmed,
          attachments,
        })
      );
    }
  }

  function interrupt() {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "interrupt" }));
    }
    setConfirmations([]);
    voiceRef.current.stopSpeaking();
  }

  function answerConfirmation(id: string, approved: boolean) {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "confirm", call_id: id, approved }));
    }
    setConfirmations((prev) => prev.filter((c) => c.id !== id));
    if (!approved) setAgentState("thinking");
  }

  async function wakeRem() {
    try {
      const status = await api.remWake();
      setRemStatus(status);
    } catch {
      /* ignore */
    }
  }

  async function cancelTask(id: string) {
    try {
      await api.cancelTask(id);
    } catch {
      /* ignore */
    }
  }

  async function deleteTask(id: string) {
    try {
      await api.deleteTask(id);
      setTasks((prev) => prev.filter((t) => t.id !== id));
    } catch {
      /* ignore */
    }
  }

  async function scanPeripherals(discover: boolean) {
    setPeriScanning(true);
    setPeriError("");
    try {
      const result = await api.scanPeripherals(discover);
      setPeripherals(result.peripherals || []);
    } catch (err) {
      setPeriError(err instanceof Error ? err.message : "Scan failed");
    } finally {
      setPeriScanning(false);
    }
  }

  async function inspectPeripheral(id: string) {
    setPeriError("");
    try {
      const next = await api.inspectPeripheral(id);
      setPeripherals((prev) => {
        const idx = prev.findIndex((p) => p.id === id);
        if (idx === -1) return [next, ...prev];
        const copy = [...prev];
        copy[idx] = next;
        return copy;
      });
    } catch (err) {
      setPeriError(err instanceof Error ? err.message : "Inspect failed");
    }
  }

  async function peripheralAction(id: string, action: string) {
    setPeriError("");
    try {
      const result = await api.peripheralAction(id, action);
      if (result.peripheral) {
        setPeripherals((prev) => {
          const idx = prev.findIndex((p) => p.id === id);
          if (idx === -1) return prev;
          const copy = [...prev];
          copy[idx] = result.peripheral as Peripheral;
          return copy;
        });
      } else {
        await api.listPeripherals().then(setPeripherals);
      }
      if (!result.ok) {
        setPeriError(result.output || `${action} failed`);
      }
    } catch (err) {
      setPeriError(err instanceof Error ? err.message : `${action} failed`);
    }
  }

  function openSystems(section: SystemsSection = "overview") {
    setSettingsSection(section);
    setSettingsOpen(true);
  }

  function askFromSystems(prompt: string) {
    setSettingsOpen(false);
    setView("console");
    void sendMessage(prompt, []);
  }

  function toggleView(next: typeof view) {
    setView((v) => (v === next ? "console" : next));
  }

  const runningTasks = tasks.filter((t) => t.status === "running").length;
  const connectedPeripherals = peripherals.filter((p) => p.connected).length;
  const importedMcpTools = mcpStatus?.imported_tool_count ?? 0;

  const remDreaming =
    !!remStatus && (remStatus.running || remStatus.phase !== "awake");

  return (
    <div className="app">
      <div className="scanlines" />
      <Sidebar
        conversations={conversations}
        currentId={currentId}
        onSelect={selectConversation}
        onNew={newConversation}
        onDelete={removeConversation}
        onOpenSettings={() => openSystems("overview")}
        online={online}
      />

      <main className="main">
        <header className="topbar">
          <div className="brand">
            <StatusOrb state={agentState} online={online} remPhase={remDreaming ? remStatus?.phase : null} />
            <div className="brand-text">
              <h1>J.A.R.V.I.S.</h1>
              <span className="subtitle">Just A Rather Very Intelligent System</span>
            </div>
          </div>
          <div className="topbar-actions">
            {voice.pttActive && (
              <span className="chip chip-on chip-ptt" title="Release Super+` to send">
                Listening
              </span>
            )}
            <button
              type="button"
              className={`chip chip-tasks ${runningTasks > 0 || view === "tasks" ? "chip-on" : ""}`}
              title="Background operations and subagents"
              onClick={() => toggleView("tasks")}
            >
              {runningTasks > 0 ? `Operations · ${runningTasks}` : "Operations"}
            </button>
            <button
              type="button"
              className={`chip chip-peri ${connectedPeripherals > 0 || view === "peripherals" ? "chip-on" : ""}`}
              title="Devices and peripherals"
              onClick={() => toggleView("peripherals")}
            >
              {connectedPeripherals > 0
                ? `Devices · ${connectedPeripherals}`
                : "Devices"}
            </button>
            <button
              type="button"
              className={`chip chip-memory ${memoryCount > 0 || view === "memory" ? "chip-on" : ""}`}
              title="What I remember"
              onClick={() => toggleView("memory")}
            >
              {memoryCount > 0 ? `Memory · ${memoryCount}` : "Memory"}
            </button>
            <button
              type="button"
              className={`chip chip-mcp ${importedMcpTools > 0 || view === "mcp" ? "chip-on" : ""}`}
              title="Imported tools and MCP connections"
              onClick={() => toggleView("mcp")}
            >
              {importedMcpTools > 0 ? `Connections · ${importedMcpTools}` : "Connections"}
            </button>
            {remDreaming && (
              <button
                type="button"
                className="chip chip-rem"
                title="Open REM sleep view"
                onClick={() => setRemViewOpen(true)}
              >
                REM · {remStatus?.phase}
              </button>
            )}
            {!remDreaming && remLogs.length > 0 && (
              <button
                type="button"
                className="chip"
                title="Replay last dream log"
                onClick={() => setRemViewOpen(true)}
              >
                Last dream
              </button>
            )}
            <button
              className={`chip ${voiceReplies ? "chip-on" : ""}`}
              onClick={() => {
                setVoiceReplies((v) => !v);
                voiceRef.current.stopSpeaking();
              }}
              title="Spoken replies, including while JARVIS is working"
            >
              {voiceReplies ? "Voice" : "Muted"}
            </button>
            <button
              type="button"
              className={`chip ${settingsOpen ? "chip-on" : ""}`}
              title="Systems configuration"
              onClick={() => openSystems("overview")}
            >
              Systems
            </button>
            <span className="state-label">
              {remDreaming ? `Dreaming · ${remStatus?.phase}` : stateLabel(agentState)}
            </span>
          </div>
        </header>

        <div className="workspace-wrap">
          <div className="workspace">
            <ChatPanel
              messages={messages}
              agentState={agentState}
              onSend={sendMessage}
              onInterrupt={interrupt}
              voice={voice}
              briefing={briefing}
              onOpenSystems={() => openSystems("overview")}
              onOpenDevices={() => toggleView("peripherals")}
            />
            <ActivityFeed
              items={activity}
              agentState={agentState}
              onClear={clearActivityMonitor}
            />
          </div>
          {view === "tasks" && (
            <HudOverlay onClose={() => setView("console")}>
              <TasksView
                tasks={tasks}
                onCancel={cancelTask}
                onDelete={deleteTask}
                onBack={() => setView("console")}
              />
            </HudOverlay>
          )}
          {view === "mcp" && (
            <HudOverlay onClose={() => setView("console")}>
              <McpView
                onBack={() => setView("console")}
                onSaved={() => {
                  api.mcpStatus().then(setMcpStatus).catch(() => {});
                }}
              />
            </HudOverlay>
          )}
          {view === "memory" && (
            <HudOverlay onClose={() => setView("console")}>
              <MemoryView
                onBack={() => {
                  setView("console");
                  refreshMemoryCount();
                }}
              />
            </HudOverlay>
          )}
          {view === "peripherals" && (
            <HudOverlay onClose={() => setView("console")}>
              <PeripheralsView
                peripherals={peripherals}
                scanning={periScanning}
                lastError={periError}
                onScan={(discover) => void scanPeripherals(discover)}
                onInspect={(id) => void inspectPeripheral(id)}
                onAction={(id, action) => void peripheralAction(id, action)}
                onAsk={(prompt) => {
                  setView("console");
                  void sendMessage(prompt, []);
                }}
                onBack={() => setView("console")}
              />
            </HudOverlay>
          )}
        </div>
      </main>

      {bootOpen && (
        <SystemsBoot
          speak={voiceReplies ? (text) => voiceRef.current.speak(text) : undefined}
          onDone={() => {
            setBootOpen(false);
            refreshBriefing();
          }}
        />
      )}

      {remViewOpen && remStatus && (
        <RemSleepView
          status={remStatus}
          logs={remLogs}
          onDismiss={() => setRemViewOpen(false)}
          onWake={() => {
            void wakeRem();
          }}
        />
      )}

      {confirmations.length > 0 && (
        <ConfirmModal request={confirmations[0]} onAnswer={answerConfirmation} />
      )}
      {settingsOpen && (
        <SettingsModal
          initialSection={settingsSection}
          onClose={() => {
            setSettingsOpen(false);
            refreshBriefing();
          }}
          onAsk={askFromSystems}
          onOpenMemoryBank={() => {
            setSettingsOpen(false);
            setView("memory");
          }}
        />
      )}
    </div>
  );
}

function stateLabel(state: AgentState): string {
  switch (state) {
    case "thinking":
      return "Analysing…";
    case "running_tool":
      return "Executing…";
    case "awaiting_confirmation":
      return "Awaiting your authorisation";
    default:
      return "Standing by";
  }
}

function stripForSpeech(text: string): string {
  return text
    .replace(/```[\s\S]*?```/g, " (code block) ")
    .replace(/[*_`#>]/g, "")
    .slice(0, 480);
}
