import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type { MemoryEntry, MemoryFiles, MemoryStats } from "../types";

const KIND_LABEL: Record<string, string> = {
  long_term: "Long-term",
  preference: "Preference",
  conversation: "Conversation",
  device: "Device",
  device_control: "Device control",
  peripheral: "Peripheral",
  rem_theme: "REM theme",
  staged: "Staged",
  short_term: "Short-term",
};

const KIND_ORDER = [
  "long_term",
  "preference",
  "conversation",
  "device",
  "device_control",
  "peripheral",
  "rem_theme",
  "staged",
  "short_term",
];

function kindLabel(kind: string): string {
  return KIND_LABEL[kind] || kind.replace(/_/g, " ");
}

function ago(ts: number): string {
  const secs = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (secs < 60) return "just now";
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

export function MemoryView({ onBack }: { onBack: () => void }) {
  const [stats, setStats] = useState<MemoryStats | null>(null);
  const [files, setFiles] = useState<MemoryFiles | null>(null);
  const [items, setItems] = useState<MemoryEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [filter, setFilter] = useState<string>("all");
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [note, setNote] = useState("");
  const [confirmWipe, setConfirmWipe] = useState<"none" | "kind" | "all">("none");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const kind = filter === "all" ? undefined : filter;
      const [st, fl, list] = await Promise.all([
        api.memoryStats(),
        api.memoryFiles(),
        api.listMemories({ kind, q: search || undefined, limit: 200 }),
      ]);
      setStats(st);
      setFiles(fl);
      setItems(list.items);
      setTotal(list.total);
      setSelected(new Set());
    } catch (err) {
      setNote(err instanceof Error ? err.message : "Failed to load memories");
    } finally {
      setLoading(false);
    }
  }, [filter, search]);

  useEffect(() => {
    void load();
  }, [load]);

  const kindFilters = useMemo(() => {
    const counts = stats?.by_kind ?? {};
    const known = KIND_ORDER.filter((k) => (counts[k] ?? 0) > 0);
    const rest = Object.keys(counts).filter((k) => !KIND_ORDER.includes(k));
    return { counts, known, rest };
  }, [stats]);

  async function removeOne(id: number) {
    setBusy(true);
    setNote("");
    try {
      await api.deleteMemory(id);
      setSelected((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
      await load();
    } catch (err) {
      setNote(err instanceof Error ? err.message : "Delete failed");
    } finally {
      setBusy(false);
    }
  }

  async function removeSelected() {
    if (selected.size === 0) return;
    setBusy(true);
    setNote("");
    try {
      const res = await api.deleteMemories([...selected]);
      setNote(`Removed ${res.deleted} memor${res.deleted === 1 ? "y" : "ies"}.`);
      await load();
    } catch (err) {
      setNote(err instanceof Error ? err.message : "Delete failed");
    } finally {
      setBusy(false);
    }
  }

  async function wipe(kindOnly: boolean) {
    setBusy(true);
    setNote("");
    try {
      const res = await api.wipeMemories({
        kinds: kindOnly && filter !== "all" ? [filter] : undefined,
        include_files: !kindOnly,
      });
      const msg = res.files_cleared
        ? `Wiped ${res.deleted} memories and cleared MEMORY.md / DREAMS.md.`
        : `Wiped ${res.deleted} memor${res.deleted === 1 ? "y" : "ies"}.`;
      setNote(msg);
      setConfirmWipe("none");
      await load();
    } catch (err) {
      setNote(err instanceof Error ? err.message : "Wipe failed");
    } finally {
      setBusy(false);
    }
  }

  function toggleSelect(id: number) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleSelectAll() {
    if (selected.size === items.length) {
      setSelected(new Set());
    } else {
      setSelected(new Set(items.map((m) => m.id)));
    }
  }

  return (
    <section className="memory-view">
      <div className="memory-view-head">
        <div className="memory-view-title">
          <h2>Memory</h2>
          <span className="memory-view-sub">
            What I remember about you and this machine — preferences, device notes, and
            long-term facts from REM sleep.
          </span>
        </div>
        <div className="memory-view-stats">
          <span className="memory-stat">
            <strong>{stats?.total ?? "—"}</strong> stored
          </span>
          {stats?.has_memory_md && (
            <span className="memory-stat memory-stat-md">MEMORY.md</span>
          )}
          <button type="button" className="btn btn-ghost" onClick={onBack}>
            Close
          </button>
        </div>
      </div>

      <div className="memory-toolbar">
        <div className="memory-filters">
          <button
            type="button"
            className={`memory-filter ${filter === "all" ? "active" : ""}`}
            onClick={() => setFilter("all")}
          >
            All {stats ? `(${stats.total})` : ""}
          </button>
          {kindFilters.known.map((k) => (
            <button
              key={k}
              type="button"
              className={`memory-filter ${filter === k ? "active" : ""}`}
              onClick={() => setFilter(k)}
            >
              {kindLabel(k)} ({kindFilters.counts[k]})
            </button>
          ))}
          {kindFilters.rest.map((k) => (
            <button
              key={k}
              type="button"
              className={`memory-filter ${filter === k ? "active" : ""}`}
              onClick={() => setFilter(k)}
            >
              {kindLabel(k)} ({kindFilters.counts[k]})
            </button>
          ))}
        </div>
        <form
          className="memory-search"
          onSubmit={(e) => {
            e.preventDefault();
            setSearch(query.trim());
          }}
        >
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search memories…"
          />
          <button type="submit" className="btn btn-ghost">
            Search
          </button>
          {search && (
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => {
                setQuery("");
                setSearch("");
              }}
            >
              Clear
            </button>
          )}
        </form>
      </div>

      <div className="memory-actions-bar">
        <label className="memory-select-all">
          <input
            type="checkbox"
            checked={items.length > 0 && selected.size === items.length}
            onChange={toggleSelectAll}
            disabled={items.length === 0 || busy}
          />
          Select all
        </label>
        <button
          type="button"
          className="btn btn-danger-outline"
          disabled={selected.size === 0 || busy}
          onClick={() => void removeSelected()}
        >
          Delete selected ({selected.size})
        </button>
        {filter !== "all" && (
          <button
            type="button"
            className="btn btn-danger-outline"
            disabled={busy}
            onClick={() => setConfirmWipe("kind")}
          >
            Wipe {kindLabel(filter)} only
          </button>
        )}
        <button
          type="button"
          className="btn btn-danger"
          disabled={busy}
          onClick={() => setConfirmWipe("all")}
        >
          Wipe all memory
        </button>
        <button type="button" className="btn btn-ghost" disabled={busy} onClick={() => void load()}>
          Refresh
        </button>
      </div>

      {confirmWipe !== "none" && (
        <div className="memory-confirm">
          <p>
            {confirmWipe === "all"
              ? "Permanently delete every stored memory and clear MEMORY.md / DREAMS.md? JARVIS will forget learned facts until new ones are recorded."
              : `Permanently delete all "${kindLabel(filter)}" memories?`}
          </p>
          <div className="memory-confirm-actions">
            <button
              type="button"
              className="btn btn-danger"
              disabled={busy}
              onClick={() => void wipe(confirmWipe === "kind")}
            >
              Confirm wipe
            </button>
            <button
              type="button"
              className="btn btn-ghost"
              disabled={busy}
              onClick={() => setConfirmWipe("none")}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {note && <p className="memory-note">{note}</p>}

      <div className="memory-view-body">
        {loading && <p className="memory-empty">Loading memory bank…</p>}
        {!loading && items.length === 0 && (
          <p className="memory-empty">
            {search
              ? "No memories match your search."
              : filter !== "all"
                ? `No ${kindLabel(filter).toLowerCase()} memories yet.`
                : "Memory bank is empty. JARVIS will store facts as you chat and when REM sleep consolidates them."}
          </p>
        )}
        {!loading &&
          items.map((m) => (
            <article key={m.id} className={`memory-card kind-${m.kind}`}>
              <div className="memory-card-head">
                <label className="memory-card-check">
                  <input
                    type="checkbox"
                    checked={selected.has(m.id)}
                    onChange={() => toggleSelect(m.id)}
                    disabled={busy}
                  />
                </label>
                <span className={`memory-kind kind-${m.kind}`}>{kindLabel(m.kind)}</span>
                <span className="memory-age">{ago(m.created_at)}</span>
                <button
                  type="button"
                  className="memory-del"
                  title="Delete this memory"
                  disabled={busy}
                  onClick={() => void removeOne(m.id)}
                >
                  ×
                </button>
              </div>
              <p className="memory-text">{m.text}</p>
            </article>
          ))}
        {!loading && total > items.length && (
          <p className="memory-more">
            Showing {items.length} of {total} — refine filters or search to narrow results.
          </p>
        )}
      </div>

      {(files?.memory_md || files?.dreams_md) && (
        <div className="memory-files">
          {files.memory_md && (
            <details className="memory-file-panel" open>
              <summary>MEMORY.md — consolidated long-term facts</summary>
              <pre>{files.memory_md}</pre>
            </details>
          )}
          {files.dreams_md && (
            <details className="memory-file-panel">
              <summary>DREAMS.md — REM dream journal</summary>
              <pre>{files.dreams_md}</pre>
            </details>
          )}
        </div>
      )}
    </section>
  );
}
