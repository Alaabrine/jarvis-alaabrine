import { useEffect, useRef, useState } from "react";
import type { BackgroundTask, TaskLogEntry } from "../types";

const STATUS_LABEL: Record<string, string> = {
  running: "Running",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
};

const LOG_TYPE_LABEL: Record<string, string> = {
  thought: "thought",
  tool_call: "tool",
  tool_result: "result",
  output: "out",
  error: "error",
  report: "report",
};

function age(createdAt: number, finishedAt: number | null): string {
  const end = finishedAt ?? Date.now() / 1000;
  const secs = Math.max(0, Math.floor(end - createdAt));
  if (secs < 60) return `${secs}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ${secs % 60}s`;
  return `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`;
}

export function TasksView({
  tasks,
  onCancel,
  onDelete,
  onBack,
}: {
  tasks: BackgroundTask[];
  onCancel: (id: string) => void;
  onDelete: (id: string) => void;
  onBack: () => void;
}) {
  const running = tasks.filter((t) => t.status === "running");
  const finished = tasks.filter((t) => t.status !== "running");
  // Ticker so elapsed times stay live while subagents run.
  const [, setTick] = useState(0);
  useEffect(() => {
    if (running.length === 0) return;
    const timer = window.setInterval(() => setTick((n) => n + 1), 1000);
    return () => window.clearInterval(timer);
  }, [running.length]);

  return (
    <section className="tasks-view">
      <div className="tasks-view-head">
        <div className="tasks-view-title">
          <h2>Subagent Operations</h2>
          <span className="tasks-view-sub">
            {running.length === 0
              ? "No subagents currently deployed."
              : `${running.length} subagent${running.length > 1 ? "s" : ""} working in the background.`}
          </span>
        </div>
        <div className="tasks-view-stats">
          <span className="tasks-stat">
            <strong>{running.length}</strong> running
          </span>
          <span className="tasks-stat">
            <strong>{finished.filter((t) => t.status === "completed").length}</strong> completed
          </span>
          <span className="tasks-stat">
            <strong>{finished.filter((t) => t.status !== "completed").length}</strong> other
          </span>
          <button type="button" className="btn btn-ghost" onClick={onBack}>
            Back to console
          </button>
        </div>
      </div>

      <div className="tasks-view-body">
        <h3 className="tasks-section-title">Active</h3>
        {running.length === 0 && (
          <p className="tasks-empty">
            Nothing running. Ask JARVIS to handle something long-running — research, builds,
            downloads — and the subagent will appear here with a live log.
          </p>
        )}
        {running.map((t) => (
          <TaskCard key={t.id} task={t} onCancel={onCancel} onDelete={onDelete} defaultOpen />
        ))}

        <h3 className="tasks-section-title">History</h3>
        {finished.length === 0 && <p className="tasks-empty">No finished tasks yet.</p>}
        {finished.map((t) => (
          <TaskCard key={t.id} task={t} onCancel={onCancel} onDelete={onDelete} />
        ))}
      </div>
    </section>
  );
}

function TaskCard({
  task,
  onCancel,
  onDelete,
  defaultOpen = false,
}: {
  task: BackgroundTask;
  onCancel: (id: string) => void;
  onDelete: (id: string) => void;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const logEnd = useRef<HTMLDivElement>(null);
  const log = task.log || [];

  useEffect(() => {
    if (open && task.status === "running") {
      logEnd.current?.scrollIntoView({ behavior: "smooth", block: "end" });
    }
  }, [log.length, open, task.status]);

  return (
    <div className={`task-card task-${task.status}`}>
      <div className="task-head" onClick={() => setOpen((o) => !o)}>
        <span className="act-chevron">{open ? "▾" : "▸"}</span>
        <span className={`badge task-kind-${task.kind}`}>
          {task.kind === "agent" ? "subagent" : "shell"}
        </span>
        <span className="task-title" title={task.goal}>
          {task.title}
        </span>
        <span className="task-age">{age(task.created_at, task.finished_at)}</span>
        <span className={`badge task-status-${task.status}`}>
          {STATUS_LABEL[task.status] ?? task.status}
        </span>
        {task.status === "running" ? (
          <button
            type="button"
            className="btn btn-ghost task-action"
            onClick={(e) => {
              e.stopPropagation();
              onCancel(task.id);
            }}
          >
            Cancel
          </button>
        ) : (
          <button
            type="button"
            className="btn btn-ghost task-action"
            onClick={(e) => {
              e.stopPropagation();
              onDelete(task.id);
            }}
            title="Remove from history"
          >
            Remove
          </button>
        )}
      </div>
      {open && (
        <div className="task-detail">
          <div className="task-goal">
            <span className="task-goal-label">goal</span>
            <span>{task.goal}</span>
          </div>
          {task.result && <div className="task-result">{task.result}</div>}
          <div className="task-log">
            {log.length === 0 && <p className="tasks-empty">No log entries yet…</p>}
            {log.map((entry, i) => (
              <LogRow key={i} entry={entry} />
            ))}
            <div ref={logEnd} />
          </div>
        </div>
      )}
    </div>
  );
}

function LogRow({ entry }: { entry: TaskLogEntry }) {
  return (
    <div className={`task-log-row task-log-${entry.type}`}>
      <span className="task-log-tag">{LOG_TYPE_LABEL[entry.type] ?? entry.type}</span>
      <span className="task-log-text">{entry.text}</span>
    </div>
  );
}
