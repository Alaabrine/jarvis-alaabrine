export interface BriefingStatus {
  userName: string;
  model: string;
  prefer: string;
  telegramReady: boolean;
  emailReady: boolean;
  peripheralCount: number;
  connectedPeripherals: number;
  mcpTools: number;
}

function timeGreeting(): string {
  const h = new Date().getHours();
  if (h < 5) return "Working late";
  if (h < 12) return "Good morning";
  if (h < 18) return "Good afternoon";
  return "Good evening";
}

export function WelcomeBriefing({
  status,
  onSuggest,
  onOpenSystems,
  onOpenDevices,
}: {
  status: BriefingStatus | null;
  onSuggest: (prompt: string) => void;
  onOpenSystems: () => void;
  onOpenDevices: () => void;
}) {
  const name = status?.userName || "Sir";
  const brain =
    status?.model
      ? `${status.prefer === "cloud" ? "Cloud" : "Local"} · ${status.model}`
      : "Awaiting a model";

  const suggestions: { label: string; prompt: string }[] = [
    { label: "What can you do?", prompt: "Give me a brief rundown of what you can do for me on this machine, as if we were starting a work session." },
    { label: "What's connected?", prompt: "What's connected to this machine right now? Summarise peripherals and anything I should know." },
    { label: "Lower the lights", prompt: "Dim the display brightness a bit." },
  ];
  if (status && !status.telegramReady) {
    suggestions.push({
      label: "Reach me on my phone",
      prompt: "Walk me through connecting Telegram so I can talk to you from my phone. Keep it simple.",
    });
  }
  if (status && !status.emailReady) {
    suggestions.push({
      label: "Set up mail",
      prompt: "Walk me through setting up email so you can send and read messages for me.",
    });
  }
  suggestions.push({
    label: "Remember a preference",
    prompt: "Remember this: I prefer concise answers unless I ask you to go deep.",
  });

  return (
    <div className="welcome briefing">
      <p className="welcome-kicker">J.A.R.V.I.S. · systems online</p>
      <p className="welcome-title">
        {timeGreeting()}, {name}.
      </p>
      <p className="welcome-sub">
        Speak or type — I am listening. Hold the microphone, or Super+` from anywhere.
        While I work, barge in, follow up, or say “stop”.
      </p>

      {status && (
        <div className="briefing-systems">
          <button type="button" className="briefing-sys" onClick={onOpenSystems} title="Open Systems">
            <span className="briefing-sys-dot on" />
            <span className="briefing-sys-label">Mind</span>
            <span className="briefing-sys-meta">{brain}</span>
          </button>
          <button type="button" className="briefing-sys" onClick={onOpenDevices} title="Open Devices">
            <span className={`briefing-sys-dot ${status.connectedPeripherals > 0 ? "on" : ""}`} />
            <span className="briefing-sys-label">Devices</span>
            <span className="briefing-sys-meta">
              {status.peripheralCount === 0
                ? "Scan pending"
                : `${status.connectedPeripherals} linked · ${status.peripheralCount} present`}
            </span>
          </button>
          <button type="button" className="briefing-sys" onClick={onOpenSystems} title="Open Systems">
            <span className={`briefing-sys-dot ${status.telegramReady ? "on" : "off"}`} />
            <span className="briefing-sys-label">Phone</span>
            <span className="briefing-sys-meta">{status.telegramReady ? "Telegram ready" : "Not linked"}</span>
          </button>
          <button type="button" className="briefing-sys" onClick={onOpenSystems} title="Open Systems">
            <span className={`briefing-sys-dot ${status.emailReady ? "on" : "off"}`} />
            <span className="briefing-sys-label">Mail</span>
            <span className="briefing-sys-meta">{status.emailReady ? "Mailbox ready" : "Not configured"}</span>
          </button>
          <button type="button" className="briefing-sys" onClick={onOpenSystems} title="Open Systems">
            <span className={`briefing-sys-dot ${status.mcpTools > 0 ? "on" : ""}`} />
            <span className="briefing-sys-label">Tools</span>
            <span className="briefing-sys-meta">
              {status.mcpTools > 0 ? `${status.mcpTools} imported` : "Ask me to add some"}
            </span>
          </button>
        </div>
      )}

      <div className="briefing-suggest">
        {suggestions.map((s) => (
          <button
            key={s.label}
            type="button"
            className="briefing-chip"
            onClick={() => onSuggest(s.prompt)}
          >
            {s.label}
          </button>
        ))}
      </div>
    </div>
  );
}
