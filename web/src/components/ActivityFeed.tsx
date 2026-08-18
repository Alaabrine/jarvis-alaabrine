import { useEffect, useRef, useState } from "react";
import type { ActivityItem, AgentState } from "../types";

const STATE_TEXT: Record<string, string> = {
  thinking: "Reasoning about the request",
  running_tool: "Executing an approved action",
  awaiting_confirmation: "Awaiting authorisation",
  interrupted: "Interrupted by user",
};

export function ActivityFeed({
  items,
  agentState,
  onClear,
}: {
  items: ActivityItem[];
  agentState: AgentState;
  onClear?: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);

  function onScroll() {
    const el = ref.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    stickToBottom.current = distance < 64;
  }

  useEffect(() => {
    const el = ref.current;
    if (!el || !stickToBottom.current) return;
    el.scrollTop = el.scrollHeight;
  }, [items]);

  return (
    <section className="activity">
      <div className="activity-header">
        <span className="activity-title">Activity Monitor</span>
        <div className="activity-header-actions">
          {onClear && items.length > 0 && (
            <button
              type="button"
              className="btn btn-ghost activity-clear"
              onClick={onClear}
              title="Clear activity for this session"
            >
              Clear
            </button>
          )}
          <span className={`pulse ${agentState !== "idle" ? "pulse-on" : ""}`} />
        </div>
      </div>
      <div className="activity-body" ref={ref} onScroll={onScroll}>
        {items.length === 0 && (
          <p className="activity-empty">Live telemetry of JARVIS's actions appears here.</p>
        )}
        {items.map((item) => (
          <ActivityRow key={item.id} item={item} />
        ))}
      </div>
    </section>
  );
}

function ActivityRow({ item }: { item: ActivityItem }) {
  const [open, setOpen] = useState(false);

  if (item.kind === "status") {
    return (
      <div className="act-row act-status">
        <span className="act-dot" />
        <span>{STATE_TEXT[item.state ?? ""] ?? item.state}</span>
      </div>
    );
  }
  if (item.kind === "error") {
    return (
      <div className="act-row act-error">
        <span className="act-icon">!</span>
        <span>{item.text}</span>
      </div>
    );
  }

  if (item.kind === "peripheral") {
    return (
      <div className={`act-card act-task ${item.ok === false ? "fail" : item.ok ? "ok" : ""}`}>
        <div className="act-card-head">
          <span className="badge badge-task">{item.state || "device"}</span>
          <span className="act-task-text">{item.text}</span>
          {item.name && <span className="act-task-id">{item.name}</span>}
        </div>
      </div>
    );
  }

  if (item.kind === "task") {
    const finished = item.state && item.state !== "running";
    return (
      <div className={`act-card act-task ${finished ? (item.ok ? "ok" : "fail") : "pending"}`}>
        <div className="act-card-head" onClick={() => setOpen((o) => !o)}>
          <span className="badge badge-task">{item.state === "running" ? "spawned" : item.state}</span>
          <span className="act-task-text">{item.text}</span>
          {item.name && <span className="act-task-id">{item.name}</span>}
        </div>
        {item.output && (
          <pre className={`act-output ${open ? "open" : ""}`} onClick={() => setOpen((o) => !o)}>
            {open ? item.output : truncate(item.output, 240)}
          </pre>
        )}
      </div>
    );
  }

  if (item.kind === "thought") {
    const reasoning = (item.reasoning || "").trim();
    const content = (item.content || "").trim();
    const hasBody = Boolean(reasoning || content);
    return (
      <div className={`act-card thought ${item.streaming ? "streaming" : ""} ${open ? "expanded" : ""}`}>
        <button
          type="button"
          className="act-card-head act-thought-toggle"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
        >
          <span className="act-chevron">{open ? "▾" : "▸"}</span>
          <span className="act-tool act-thought-label">reasoning</span>
          {item.streaming && <span className="badge badge-stream">live</span>}
          <span className="act-state">
            {item.streaming ? "thinking…" : hasBody ? (open ? "hide" : "show") : "empty"}
          </span>
        </button>
        {open && (
          <div className="act-thought-body">
            {reasoning && <pre className="act-reasoning">{reasoning}</pre>}
            {content && reasoning && <div className="act-thought-sep" />}
            {content && <pre className="act-narration">{content}</pre>}
            {!hasBody && item.streaming && (
              <pre className="act-reasoning act-waiting">Awaiting tokens…</pre>
            )}
          </div>
        )}
      </div>
    );
  }

  // tool_call (possibly with attached result)
  const resolved = item.output !== undefined;
  return (
    <div className={`act-card ${item.dangerous ? "danger" : ""} ${resolved ? (item.ok ? "ok" : "fail") : "pending"}`}>
      <div className="act-card-head" onClick={() => setOpen((o) => !o)}>
        <span className="act-tool">{item.name}</span>
        {item.dangerous && !item.auto_approved && <span className="badge badge-danger">gated</span>}
        {item.auto_approved && <span className="badge badge-auto">auto</span>}
        <span className="act-state">
          {resolved ? (item.ok ? "done" : "failed") : "running…"}
        </span>
      </div>
      {item.args && Object.keys(item.args).length > 0 && (
        <div className="act-args">
          {Object.entries(item.args).map(([k, v]) => (
            <div key={k} className="act-arg">
              <span className="arg-key">{k}</span>
              <span className="arg-val">{truncate(String(v), 160)}</span>
            </div>
          ))}
        </div>
      )}
      {resolved && (
        <pre className={`act-output ${open ? "open" : ""}`} onClick={() => setOpen((o) => !o)}>
          {open ? item.output : truncate(item.output ?? "", 240)}
        </pre>
      )}
    </div>
  );
}

function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, n) + " …" : s;
}
