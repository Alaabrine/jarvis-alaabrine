import { useEffect, useRef } from "react";
import type { RemLogEntry, RemStatus } from "../types";

const PHASE_LABEL: Record<string, string> = {
  dreaming: "Entering sleep",
  light: "Light sleep",
  rem: "REM dreaming",
  deep: "Deep consolidation",
  awake: "Waking",
};

export function RemSleepView({
  status,
  logs,
  onDismiss,
  onWake,
}: {
  status: RemStatus;
  logs: RemLogEntry[];
  onDismiss: () => void;
  onWake: () => void;
}) {
  const logEnd = useRef<HTMLDivElement>(null);
  const phase = status.phase;
  const active = status.running || phase !== "awake";

  useEffect(() => {
    logEnd.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [logs]);

  return (
    <div className={`rem-sleep ${active ? "rem-active" : "rem-afterglow"}`} role="dialog" aria-label="REM sleep">
      <div className="rem-veil" />
      <div className="rem-stars" aria-hidden />
      <div className="rem-stars rem-stars-2" aria-hidden />

      <div className="rem-panel">
        <div className="rem-hero">
          <div className={`rem-reactor rem-phase-${phase}`}>
            <div className="rem-reactor-glow" />
            <div className="rem-reactor-ring rem-ring-a" />
            <div className="rem-reactor-ring rem-ring-b" />
            <div className="rem-reactor-ring rem-ring-c" />
            <div className="rem-reactor-core">
              <div className="rem-reactor-iris" />
              <div className="rem-reactor-lid" />
            </div>
            <div className="rem-dream-wisps" aria-hidden>
              <span />
              <span />
              <span />
              <span />
            </div>
          </div>

          <div className="rem-copy">
            <p className="rem-kicker">Memory consolidation</p>
            <h2 className="rem-title">
              {active ? "JARVIS is dreaming" : "Dream cycle complete"}
            </h2>
            <p className="rem-phase">{PHASE_LABEL[phase] ?? phase}</p>
            <p className="rem-sub">
              {active
                ? "Arc reactor dimmed. Short-term traces are being sorted into long-term memory."
                : status.last_result || "Standing by for the next idle cycle."}
            </p>
          </div>
        </div>

        <div className="rem-log-panel">
          <div className="rem-log-head">
            <span>Consolidation log</span>
            <span className="rem-log-count">{logs.length} events</span>
          </div>
          <div className="rem-log-body">
            {logs.length === 0 && (
              <p className="rem-log-empty">Awaiting dream telemetry…</p>
            )}
            {logs.map((entry) => (
              <div
                key={entry.id}
                className={`rem-log-row rem-log-${entry.level} rem-log-phase-${entry.phase}`}
              >
                <span className="rem-log-phase-tag">{entry.phase}</span>
                <span className="rem-log-text">{entry.text}</span>
              </div>
            ))}
            <div ref={logEnd} />
          </div>
        </div>

        <div className="rem-actions">
          {active ? (
            <button type="button" className="btn btn-primary" onClick={onWake}>
              Wake JARVIS
            </button>
          ) : (
            <button type="button" className="btn btn-primary" onClick={onDismiss}>
              Return to console
            </button>
          )}
          {active && (
            <button type="button" className="btn btn-ghost" onClick={onDismiss}>
              Hide overlay
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
