import { useMemo, useState } from "react";
import type { Peripheral, PeripheralKind } from "../types";

const KIND_LABEL: Record<string, string> = {
  bluetooth: "Bluetooth",
  usb: "USB",
  audio: "Audio",
  display: "Displays",
  hid: "Input",
  camera: "Cameras",
  printer: "Printers",
  storage: "Storage",
  wifi: "Wi-Fi",
  network: "Network",
  radio: "Radios",
};

const KIND_ORDER: PeripheralKind[] = [
  "bluetooth",
  "usb",
  "audio",
  "display",
  "hid",
  "camera",
  "printer",
  "storage",
  "wifi",
  "network",
  "radio",
];

function ago(ts?: number): string {
  if (!ts) return "";
  const secs = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (secs < 60) return "just now";
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

function statusOf(p: Peripheral): "connected" | "paired" | "present" | "offline" {
  if (!p.available && !p.connected) return "offline";
  if (p.connected) return "connected";
  if (p.paired) return "paired";
  return "present";
}

export function PeripheralsView({
  peripherals,
  scanning,
  lastError,
  onScan,
  onInspect,
  onAction,
  onAsk,
  onBack,
}: {
  peripherals: Peripheral[];
  scanning: boolean;
  lastError: string;
  onScan: (discover: boolean) => void;
  onInspect: (id: string) => void;
  onAction: (id: string, action: string) => void;
  onAsk: (prompt: string) => void;
  onBack: () => void;
}) {
  const [filter, setFilter] = useState<string>("all");
  const [busyId, setBusyId] = useState<string | null>(null);

  const visible = useMemo(() => {
    if (filter === "all") return peripherals;
    if (filter === "connected") return peripherals.filter((p) => p.connected);
    return peripherals.filter((p) => p.kind === filter);
  }, [peripherals, filter]);

  const grouped = useMemo(() => {
    const map = new Map<string, Peripheral[]>();
    for (const p of visible) {
      const k = p.kind || "other";
      const list = map.get(k) || [];
      list.push(p);
      map.set(k, list);
    }
    const keys = [
      ...KIND_ORDER.filter((k) => map.has(k)),
      ...[...map.keys()].filter((k) => !KIND_ORDER.includes(k)),
    ];
    return keys.map((k) => [k, map.get(k) || []] as const);
  }, [visible]);

  const connected = peripherals.filter((p) => p.connected).length;
  const present = peripherals.filter((p) => p.available !== false).length;

  async function run(id: string, fn: () => void | Promise<void>) {
    setBusyId(id);
    try {
      await fn();
    } finally {
      setBusyId(null);
    }
  }

  return (
    <section className="tasks-view peri-view">
      <div className="tasks-view-head">
        <div className="tasks-view-title">
          <h2>Peripherals</h2>
          <span className="tasks-view-sub">
            {present === 0
              ? "No devices inventoried yet — run a scan."
              : `${present} present · ${connected} connected. JARVIS learns how to control them.`}
          </span>
        </div>
        <div className="tasks-view-stats">
          <span className="tasks-stat">
            <strong>{connected}</strong> linked
          </span>
          <span className="tasks-stat">
            <strong>{present}</strong> present
          </span>
          <button
            type="button"
            className="btn btn-secondary"
            disabled={scanning}
            onClick={() => onScan(false)}
          >
            {scanning ? "Scanning…" : "Rescan"}
          </button>
          <button
            type="button"
            className="btn btn-ghost"
            disabled={scanning}
            title="Inquire nearby unpaired Bluetooth devices"
            onClick={() => onScan(true)}
          >
            Discover
          </button>
          <button type="button" className="btn btn-ghost" onClick={onBack}>
            Back to console
          </button>
        </div>
      </div>

      <div className="peri-filters">
        <button
          type="button"
          className={`peri-chip ${filter === "all" ? "on" : ""}`}
          onClick={() => setFilter("all")}
        >
          All
        </button>
        <button
          type="button"
          className={`peri-chip ${filter === "connected" ? "on" : ""}`}
          onClick={() => setFilter("connected")}
        >
          Connected
        </button>
        {KIND_ORDER.filter((k) => peripherals.some((p) => p.kind === k)).map((k) => (
          <button
            key={k}
            type="button"
            className={`peri-chip ${filter === k ? "on" : ""}`}
            onClick={() => setFilter(k)}
          >
            {KIND_LABEL[k] || k}
          </button>
        ))}
      </div>

      {lastError && <p className="peri-error">{lastError}</p>}

      <div className="tasks-view-body">
        {visible.length === 0 && (
          <p className="tasks-empty">
            Nothing in this filter. Ask JARVIS to connect headphones, list USB devices, or
            scan for nearby Bluetooth — or click Rescan.
          </p>
        )}
        {grouped.map(([kind, items]) => (
          <div key={kind} className="peri-group">
            <h3 className="tasks-section-title">
              {KIND_LABEL[kind] || kind} · {items.length}
            </h3>
            {items.map((p) => (
              <PeripheralCard
                key={p.id}
                p={p}
                busy={busyId === p.id}
                onInspect={() => void run(p.id, () => onInspect(p.id))}
                onAction={(action) => void run(p.id, () => onAction(p.id, action))}
                onAsk={onAsk}
              />
            ))}
          </div>
        ))}
      </div>
    </section>
  );
}

function PeripheralCard({
  p,
  busy,
  onInspect,
  onAction,
  onAsk,
}: {
  p: Peripheral;
  busy: boolean;
  onInspect: () => void;
  onAction: (action: string) => void;
  onAsk: (prompt: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const status = statusOf(p);
  const connectable = ["bluetooth", "wifi", "storage", "network"].includes(p.kind);
  const hints = p.control_hints || [];
  const facts = p.facts || [];

  return (
    <div className={`task-card peri-card peri-${status}`}>
      <div className="task-head" onClick={() => setOpen((o) => !o)}>
        <span className={`peri-dot peri-dot-${status}`} title={status} />
        <span className="task-title">{p.name}</span>
        <span className={`badge peri-status-${status}`}>{status}</span>
        {p.kind === "bluetooth" && p.paired && status !== "connected" && (
          <span className="badge task-kind-agent">paired</span>
        )}
        <span className="task-age">{ago(p.last_seen)}</span>
      </div>
      <div className="peri-card-actions">
        {connectable && status !== "connected" && status !== "offline" && (
          <button
            type="button"
            className="btn btn-secondary task-action"
            disabled={busy}
            onClick={() => onAction("connect")}
          >
            Connect
          </button>
        )}
        {connectable && status === "connected" && (
          <button
            type="button"
            className="btn btn-ghost task-action"
            disabled={busy}
            onClick={() => onAction("disconnect")}
          >
            Disconnect
          </button>
        )}
        {p.kind === "bluetooth" && !p.paired && status !== "offline" && (
          <button
            type="button"
            className="btn btn-ghost task-action"
            disabled={busy}
            onClick={() => onAction("pair")}
          >
            Pair
          </button>
        )}
        <button
          type="button"
          className="btn btn-ghost task-action"
          disabled={busy}
          onClick={onInspect}
        >
          Learn
        </button>
        <button
          type="button"
          className="btn btn-ghost task-action"
          onClick={() =>
            onAsk(`Tell me about the ${p.kind} device "${p.name}" and how you can control it.`)
          }
        >
          Ask JARVIS
        </button>
      </div>
      {open && (
        <div className="task-detail">
          <div className="peri-meta">
            <span>
              <em>id</em> {p.id}
            </span>
            {p.address && (
              <span>
                <em>address</em> {p.address}
              </span>
            )}
            {(p.vendor || p.product) && (
              <span>
                <em>hardware</em> {[p.vendor, p.product].filter(Boolean).join(" · ")}
              </span>
            )}
          </div>
          {hints.length > 0 && (
            <ul className="peri-hints">
              {hints.map((h) => (
                <li key={h}>{h}</li>
              ))}
            </ul>
          )}
          {facts.length > 0 && (
            <div className="peri-facts">
              {facts.slice(-8).map((f) => (
                <p key={f}>{f}</p>
              ))}
            </div>
          )}
          {hints.length === 0 && facts.length === 0 && (
            <p className="tasks-empty">No control hints yet — click Learn.</p>
          )}
        </div>
      )}
    </div>
  );
}
