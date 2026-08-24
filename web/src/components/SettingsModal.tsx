import { useEffect, useState } from "react";
import { api } from "../api";
import type { Device, EmailProfile, JarvisConfig, MemoryStats, RemStatus } from "../types";

export type SystemsSection =
  | "overview"
  | "identity"
  | "brain"
  | "permissions"
  | "memory"
  | "mail"
  | "phone"
  | "tools"
  | "hardware";

const SECTIONS: { id: SystemsSection; label: string }[] = [
  { id: "overview", label: "All systems" },
  { id: "identity", label: "Identity" },
  { id: "brain", label: "Mind" },
  { id: "permissions", label: "Authorisation" },
  { id: "memory", label: "Memory" },
  { id: "mail", label: "Mail" },
  { id: "phone", label: "Phone" },
  { id: "tools", label: "Tools" },
  { id: "hardware", label: "Hardware" },
];

const LLM_PRESETS = [
  {
    id: "odysseus",
    label: "Odysseus / Ollama",
    prefer: "local" as const,
    base_url: "http://localhost:11434/v1",
    model: "qwen2.5:latest",
    api_key: "local",
  },
  {
    id: "openai",
    label: "OpenAI",
    prefer: "cloud" as const,
    fallback_base_url: "https://api.openai.com/v1",
    fallback_model: "gpt-4o-mini",
  },
  {
    id: "openrouter",
    label: "OpenRouter",
    prefer: "cloud" as const,
    fallback_base_url: "https://openrouter.ai/api/v1",
    fallback_model: "openai/gpt-4o-mini",
  },
];

function emptyProfile(id?: string): EmailProfile {
  const pid = id || `profile-${Date.now().toString(36)}`;
  return {
    id: pid,
    name: "New account",
    smtp_host: "",
    smtp_port: 587,
    smtp_user: "",
    smtp_password: "",
    from_address: "",
    imap_host: "",
    imap_port: 993,
    imap_user: "",
    imap_password: "",
  };
}

function normalizeAccounts(c: JarvisConfig): JarvisConfig["email_accounts"] {
  const profiles =
    c.email_accounts?.profiles?.length
      ? c.email_accounts.profiles.map((p) => ({
          ...emptyProfile(p.id),
          ...p,
          smtp_password: p.smtp_password === "********" ? "" : p.smtp_password || "",
          imap_password: p.imap_password === "********" ? "" : p.imap_password || "",
        }))
      : c.email
        ? [
            {
              ...emptyProfile("default"),
              ...c.email,
              id: "default",
              name: c.email.name || "Default",
              smtp_password:
                c.email.smtp_password === "********" ? "" : c.email.smtp_password || "",
              imap_password:
                c.email.imap_password === "********" ? "" : c.email.imap_password || "",
            },
          ]
        : [emptyProfile("default")];
  const default_id =
    c.email_accounts?.default_id && profiles.some((p) => p.id === c.email_accounts.default_id)
      ? c.email_accounts.default_id
      : profiles[0].id;
  return { default_id, profiles };
}

export function SettingsModal({
  onClose,
  onOpenMemoryBank,
  onAsk,
  initialSection = "overview",
}: {
  onClose: () => void;
  onOpenMemoryBank?: () => void;
  onAsk?: (prompt: string) => void;
  initialSection?: SystemsSection;
}) {
  const [cfg, setCfg] = useState<JarvisConfig | null>(null);
  const [devices, setDevices] = useState<Device[]>([]);
  const [remStatus, setRemStatus] = useState<RemStatus | null>(null);
  const [memoryStats, setMemoryStats] = useState<MemoryStats | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [remRunning, setRemRunning] = useState(false);
  const [remNote, setRemNote] = useState("");
  const [memoryWiping, setMemoryWiping] = useState(false);
  const [memoryNote, setMemoryNote] = useState("");
  const [confirmWipeMemory, setConfirmWipeMemory] = useState(false);
  const [deviceRefreshing, setDeviceRefreshing] = useState(false);
  const [activeProfileId, setActiveProfileId] = useState<string>("default");
  const [section, setSection] = useState<SystemsSection>(initialSection);
  const [showEmbeddings, setShowEmbeddings] = useState(false);

  useEffect(() => {
    api
      .getConfig()
      .then((c) => {
        const email_accounts = normalizeAccounts(c);
        setCfg({
          ...c,
          email_accounts,
          email:
            email_accounts.profiles.find((p) => p.id === email_accounts.default_id) ||
            email_accounts.profiles[0],
          permissions: {
            ...c.permissions,
            auto_approve: c.permissions?.auto_approve ?? false,
            sudo_password: c.permissions?.sudo_password ?? "",
          },
          rem: {
            enabled: c.rem?.enabled ?? true,
            idle_minutes: c.rem?.idle_minutes ?? 15,
            min_interval_minutes: c.rem?.min_interval_minutes ?? 60,
          },
          device: {
            auto_learn: c.device?.auto_learn ?? true,
            refresh_hours: c.device?.refresh_hours ?? 6,
            scan_peripherals: c.device?.scan_peripherals ?? true,
            peripheral_refresh_minutes: c.device?.peripheral_refresh_minutes ?? 15,
          },
          telegram: {
            enabled: c.telegram?.enabled ?? false,
            bot_token: c.telegram?.bot_token === "********" ? "" : c.telegram?.bot_token || "",
            bot_token_configured: c.telegram?.bot_token_configured ?? false,
            allowed_chat_ids: c.telegram?.allowed_chat_ids ?? [],
            allowed_user_ids: c.telegram?.allowed_user_ids ?? [],
            notify_tools: c.telegram?.notify_tools ?? true,
          },
          mcp: {
            enabled: c.mcp?.enabled ?? true,
            servers: (c.mcp?.servers ?? []).map((s) => ({
              ...s,
              headers: Object.fromEntries(
                Object.entries(s.headers ?? {}).map(([k, v]) => [k, v === "********" ? "" : v])
              ),
              headers_configured: s.headers_configured,
              env: Object.fromEntries(
                Object.entries(s.env ?? {}).map(([k, v]) => [k, v === "********" ? "" : v])
              ),
              env_configured: s.env_configured,
            })),
          },
        });
        setActiveProfileId(email_accounts.default_id);
      })
      .catch(() => {});
    api.listDevices().then(setDevices).catch(() => {});
    api.remStatus().then(setRemStatus).catch(() => {});
    api.memoryStats().then(setMemoryStats).catch(() => {});
  }, []);

  if (!cfg) {
    return (
      <div className="modal-overlay" onClick={onClose}>
        <div className="modal" onClick={(e) => e.stopPropagation()}>
          <p>Bringing systems online…</p>
        </div>
      </div>
    );
  }

  const profiles = cfg.email_accounts?.profiles ?? [];
  const activeProfile =
    profiles.find((p) => p.id === activeProfileId) || profiles[0] || emptyProfile("default");

  function setLLM(key: keyof JarvisConfig["llm"], value: string | number) {
    setCfg((c) => (c ? { ...c, llm: { ...c.llm, [key]: value } } : c));
  }
  function setTop(key: keyof JarvisConfig, value: string) {
    setCfg((c) => (c ? { ...c, [key]: value } : c));
  }
  function setPerm(key: keyof JarvisConfig["permissions"], value: string | boolean) {
    setCfg((c) =>
      c
        ? {
            ...c,
            permissions: {
              auto_approve: c.permissions?.auto_approve ?? false,
              sudo_password: c.permissions?.sudo_password ?? "",
              sudo_configured: c.permissions?.sudo_configured,
              [key]: value,
            },
          }
        : c
    );
  }
  function setRem(key: keyof JarvisConfig["rem"], value: boolean | number) {
    setCfg((c) =>
      c
        ? {
            ...c,
            rem: {
              enabled: c.rem?.enabled ?? true,
              idle_minutes: c.rem?.idle_minutes ?? 15,
              min_interval_minutes: c.rem?.min_interval_minutes ?? 60,
              [key]: value,
            },
          }
        : c
    );
  }
  function setTelegram(
    key: keyof NonNullable<JarvisConfig["telegram"]>,
    value: string | boolean | string[]
  ) {
    setCfg((c) =>
      c
        ? {
            ...c,
            telegram: {
              enabled: c.telegram?.enabled ?? false,
              bot_token: c.telegram?.bot_token ?? "",
              bot_token_configured: c.telegram?.bot_token_configured,
              allowed_chat_ids: c.telegram?.allowed_chat_ids ?? [],
              allowed_user_ids: c.telegram?.allowed_user_ids ?? [],
              notify_tools: c.telegram?.notify_tools ?? true,
              [key]: value,
            },
          }
        : c
    );
  }

  function updateActiveProfile(patch: Partial<EmailProfile>) {
    setCfg((c) => {
      if (!c) return c;
      const list = (c.email_accounts?.profiles ?? []).map((p) =>
        p.id === activeProfile.id ? { ...p, ...patch } : p
      );
      const default_id = c.email_accounts?.default_id || list[0]?.id || "default";
      const email = list.find((p) => p.id === default_id) || list[0];
      return {
        ...c,
        email_accounts: { default_id, profiles: list },
        email: email || c.email,
      };
    });
  }

  function addProfile() {
    const profile = emptyProfile();
    setCfg((c) => {
      if (!c) return c;
      const list = [...(c.email_accounts?.profiles ?? []), profile];
      return {
        ...c,
        email_accounts: {
          default_id: c.email_accounts?.default_id || profile.id,
          profiles: list,
        },
      };
    });
    setActiveProfileId(profile.id);
  }

  function removeActiveProfile() {
    setCfg((c) => {
      if (!c) return c;
      const list = (c.email_accounts?.profiles ?? []).filter((p) => p.id !== activeProfile.id);
      const next = list.length ? list : [emptyProfile("default")];
      let default_id = c.email_accounts?.default_id || next[0].id;
      if (!next.some((p) => p.id === default_id)) default_id = next[0].id;
      setActiveProfileId(default_id);
      return {
        ...c,
        email_accounts: { default_id, profiles: next },
        email: next.find((p) => p.id === default_id) || next[0],
      };
    });
  }

  function setDefaultProfile(id: string) {
    setCfg((c) => {
      if (!c) return c;
      const list = c.email_accounts?.profiles ?? [];
      const email = list.find((p) => p.id === id) || list[0];
      return {
        ...c,
        email_accounts: { default_id: id, profiles: list },
        email: email || c.email,
      };
    });
  }

  async function save() {
    if (!cfg) return;
    setSaving(true);
    try {
      const accounts = cfg.email_accounts;
      await api.saveConfig({
        user_name: cfg.user_name,
        hotkey: cfg.hotkey,
        ptt_hotkey: cfg.ptt_hotkey || "Super+Backquote",
        searxng_url: cfg.searxng_url,
        llm: cfg.llm,
        email_accounts: {
          default_id: accounts.default_id,
          profiles: accounts.profiles.map((p) => ({
            id: p.id,
            name: p.name,
            smtp_host: p.smtp_host,
            smtp_port: p.smtp_port,
            smtp_user: p.smtp_user,
            smtp_password: p.smtp_password || "********",
            from_address: p.from_address,
            imap_host: p.imap_host,
            imap_port: p.imap_port,
            imap_user: p.imap_user,
            imap_password: p.imap_password || "********",
          })),
        },
        permissions: {
          auto_approve: cfg.permissions?.auto_approve ?? false,
          sudo_password: cfg.permissions?.sudo_password ?? "",
        },
        rem: {
          enabled: cfg.rem?.enabled ?? true,
          idle_minutes: cfg.rem?.idle_minutes ?? 15,
          min_interval_minutes: cfg.rem?.min_interval_minutes ?? 60,
        },
        device: {
          auto_learn: cfg.device?.auto_learn ?? true,
          refresh_hours: cfg.device?.refresh_hours ?? 6,
          scan_peripherals: cfg.device?.scan_peripherals ?? true,
          peripheral_refresh_minutes: cfg.device?.peripheral_refresh_minutes ?? 15,
        },
        telegram: {
          enabled: cfg.telegram?.enabled ?? false,
          bot_token: cfg.telegram?.bot_token || "********",
          allowed_chat_ids: cfg.telegram?.allowed_chat_ids ?? [],
          allowed_user_ids: cfg.telegram?.allowed_user_ids ?? [],
          notify_tools: cfg.telegram?.notify_tools ?? true,
        },
        mcp: {
          enabled: cfg.mcp?.enabled ?? true,
          servers: (cfg.mcp?.servers ?? []).map((s) => ({
            id: s.id,
            name: s.name,
            url: s.url,
            enabled: s.enabled,
            headers: Object.fromEntries(
              Object.entries(s.headers ?? {})
                .filter(([k, v]) => k.trim() && (v || s.headers_configured))
                .map(([k, v]) => [k, v || "********"])
            ),
            // Local (stdio) servers JARVIS installed itself: pass these through, or
            // saving settings would unconfigure them.
            transport: s.transport ?? (s.command ? "stdio" : "http"),
            command: s.command ?? "",
            args: s.args ?? [],
            env: Object.fromEntries(
              Object.entries(s.env ?? {})
                .filter(([k, v]) => k.trim() && (v || s.env_configured))
                .map(([k, v]) => [k, v || "********"])
            ),
            cwd: s.cwd ?? "",
          })),
        },
      });
      const refreshed = await api.getConfig();
      const email_accounts = normalizeAccounts(refreshed);
      setCfg({
        ...refreshed,
        email_accounts,
        email:
          email_accounts.profiles.find((p) => p.id === email_accounts.default_id) ||
          email_accounts.profiles[0],
        rem: {
          enabled: refreshed.rem?.enabled ?? true,
          idle_minutes: refreshed.rem?.idle_minutes ?? 15,
          min_interval_minutes: refreshed.rem?.min_interval_minutes ?? 60,
        },
        telegram: {
          enabled: refreshed.telegram?.enabled ?? false,
          bot_token:
            refreshed.telegram?.bot_token === "********"
              ? ""
              : refreshed.telegram?.bot_token || "",
          bot_token_configured: refreshed.telegram?.bot_token_configured ?? false,
          allowed_chat_ids: refreshed.telegram?.allowed_chat_ids ?? [],
          allowed_user_ids: refreshed.telegram?.allowed_user_ids ?? [],
          notify_tools: refreshed.telegram?.notify_tools ?? true,
        },
      });
      setActiveProfileId((id) =>
        email_accounts.profiles.some((p) => p.id === id) ? id : email_accounts.default_id
      );
      const status = await api.remStatus();
      setRemStatus(status);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } finally {
      setSaving(false);
    }
  }

  async function runRemNow() {
    setRemRunning(true);
    setRemNote("");
    try {
      const res = await api.remRun();
      setRemNote(res.message || (res.ok ? "Dream cycle complete." : "REM busy."));
      const status = await api.remStatus();
      setRemStatus(status);
    } catch (err) {
      setRemNote(err instanceof Error ? err.message : "REM run failed");
    } finally {
      setRemRunning(false);
    }
  }

  async function wipeAllMemory() {
    setMemoryWiping(true);
    setMemoryNote("");
    try {
      const res = await api.wipeMemories({ include_files: true });
      setMemoryNote(`Wiped ${res.deleted} memories and cleared consolidated files.`);
      setConfirmWipeMemory(false);
      const stats = await api.memoryStats();
      setMemoryStats(stats);
    } catch (err) {
      setMemoryNote(err instanceof Error ? err.message : "Memory wipe failed");
    } finally {
      setMemoryWiping(false);
    }
  }

  const isDefault = activeProfile.id === cfg.email_accounts.default_id;
  const mailReady = Boolean(
    activeProfile.smtp_host && (activeProfile.from_address || activeProfile.smtp_user)
  );
  const phoneReady = Boolean(
    cfg.telegram?.enabled &&
      (cfg.telegram.bot_token_configured || (cfg.telegram.bot_token && cfg.telegram.bot_token !== "********"))
  );
  const brainLabel =
    cfg.llm.prefer === "cloud"
      ? `Cloud · ${cfg.llm.fallback_model || "unset"}`
      : `Local · ${cfg.llm.model || "unset"}`;

  function applyPreset(preset: (typeof LLM_PRESETS)[number]) {
    setCfg((c) => {
      if (!c) return c;
      if (preset.prefer === "local") {
        return {
          ...c,
          llm: {
            ...c.llm,
            prefer: "local",
            base_url: preset.base_url,
            model: preset.model,
            api_key: preset.api_key || c.llm.api_key,
          },
        };
      }
      return {
        ...c,
        llm: {
          ...c.llm,
          prefer: "cloud",
          fallback_base_url: preset.fallback_base_url || c.llm.fallback_base_url,
          fallback_model: preset.fallback_model || c.llm.fallback_model,
        },
      };
    });
    setSection("brain");
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal settings-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <div>
            <h2>Systems</h2>
            <p className="settings-head-sub">Tell me what you need, or adjust a subsystem here.</p>
          </div>
          <button className="modal-close" onClick={onClose}>
            ×
          </button>
        </div>

        <div className="settings-body">
          <nav className="settings-nav" aria-label="Systems">
            {SECTIONS.map((s) => (
              <button
                key={s.id}
                type="button"
                className={`settings-nav-item ${section === s.id ? "on" : ""}`}
                onClick={() => setSection(s.id)}
              >
                {s.label}
              </button>
            ))}
          </nav>

          <div className="settings-scroll">
            {section === "overview" && (
              <section className="settings-section">
                <h3>Status</h3>
                <p className="section-note">
                  I am already listening. Configure only what you need — or ask me to walk you through it.
                </p>
                <div className="systems-grid">
                  <button type="button" className="sys-card" onClick={() => setSection("identity")}>
                    <span className="sys-card-dot on" />
                    <strong>Identity</strong>
                    <span>{cfg.user_name || "Sir"}</span>
                  </button>
                  <button type="button" className="sys-card" onClick={() => setSection("brain")}>
                    <span className="sys-card-dot on" />
                    <strong>Mind</strong>
                    <span>{brainLabel}</span>
                  </button>
                  <button type="button" className="sys-card" onClick={() => setSection("permissions")}>
                    <span className={`sys-card-dot ${cfg.permissions?.auto_approve ? "warn" : "on"}`} />
                    <strong>Authorisation</strong>
                    <span>{cfg.permissions?.auto_approve ? "Auto-approve on" : "Irreversible only"}</span>
                  </button>
                  <button type="button" className="sys-card" onClick={() => setSection("memory")}>
                    <span className={`sys-card-dot ${cfg.rem?.enabled ? "on" : "off"}`} />
                    <strong>Memory</strong>
                    <span>
                      {cfg.rem?.enabled ? "REM sleep on" : "REM off"}
                      {memoryStats ? ` · ${memoryStats.total} facts` : ""}
                    </span>
                  </button>
                  <button type="button" className="sys-card" onClick={() => setSection("mail")}>
                    <span className={`sys-card-dot ${mailReady ? "on" : "off"}`} />
                    <strong>Mail</strong>
                    <span>{mailReady ? activeProfile.name || "Ready" : "Not configured"}</span>
                  </button>
                  <button type="button" className="sys-card" onClick={() => setSection("phone")}>
                    <span className={`sys-card-dot ${phoneReady ? "on" : "off"}`} />
                    <strong>Phone</strong>
                    <span>{phoneReady ? "Telegram ready" : "Not linked"}</span>
                  </button>
                  <button type="button" className="sys-card" onClick={() => setSection("tools")}>
                    <span className={`sys-card-dot ${cfg.mcp?.enabled ? "on" : "off"}`} />
                    <strong>Tools</strong>
                    <span>{cfg.mcp?.enabled ? "MCP endpoint on" : "MCP off"}</span>
                  </button>
                  <button type="button" className="sys-card" onClick={() => setSection("hardware")}>
                    <span className={`sys-card-dot ${devices.length ? "on" : ""}`} />
                    <strong>Hardware</strong>
                    <span>{devices.length ? `${devices.length} profile${devices.length === 1 ? "" : "s"}` : "Learning…"}</span>
                  </button>
                </div>
                {onAsk && (
                  <div className="systems-ask">
                    <p className="section-note">Prefer to talk? I can set this up with you.</p>
                    <div className="settings-row-actions">
                      {!mailReady && (
                        <button
                          type="button"
                          className="btn btn-ghost"
                          onClick={() =>
                            onAsk("Walk me through setting up email so you can send and read messages for me.")
                          }
                        >
                          Set up mail
                        </button>
                      )}
                      {!phoneReady && (
                        <button
                          type="button"
                          className="btn btn-ghost"
                          onClick={() =>
                            onAsk("Walk me through connecting Telegram so I can talk to you from my phone. Keep it simple.")
                          }
                        >
                          Link my phone
                        </button>
                      )}
                      <button
                        type="button"
                        className="btn btn-ghost"
                        onClick={() =>
                          onAsk("Find useful MCP servers for me and install the ones I should have.")
                        }
                      >
                        Add tools
                      </button>
                    </div>
                  </div>
                )}
              </section>
            )}

            {section === "identity" && (
          <section className="settings-section">
            <h3>Identity</h3>
            <Field label="Address me as">
              <input value={cfg.user_name} onChange={(e) => setTop("user_name", e.target.value)} />
            </Field>
            <Field label="Global hotkey (desktop app)">
              <input value={cfg.hotkey} onChange={(e) => setTop("hotkey", e.target.value)} placeholder="Ctrl+Alt+J" />
            </Field>
            <Field label="Push-to-talk (desktop app)">
              <input
                value={cfg.ptt_hotkey || "Super+Backquote"}
                onChange={(e) => setTop("ptt_hotkey", e.target.value)}
                placeholder="Super+Backquote"
              />
            </Field>
            <p className="section-note">
              Hold Super+` anywhere to speak to me. Requires the desktop app. Restart it
              after changing hotkeys.
            </p>
          </section>
            )}

            {section === "brain" && (
              <>
          <section className="settings-section">
            <h3>Mind</h3>
            <p className="section-note">
              Where I think. Local is Odysseus / Ollama / vLLM on this machine. Cloud is
              an API I use if you prefer it, or if local is unreachable.
            </p>
            <div className="preset-row">
              {LLM_PRESETS.map((p) => (
                <button
                  key={p.id}
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => applyPreset(p)}
                >
                  {p.label}
                </button>
              ))}
            </div>
            <Field label="Prefer">
              <select value={cfg.llm.prefer} onChange={(e) => setLLM("prefer", e.target.value)}>
                <option value="local">This machine (local)</option>
                <option value="cloud">Cloud API</option>
              </select>
            </Field>
            <p className="section-note">
              Local endpoint — Odysseus, Ollama, vLLM, or llama.cpp.
            </p>
            <Field label="Base URL">
              <input value={cfg.llm.base_url} onChange={(e) => setLLM("base_url", e.target.value)} placeholder="http://localhost:11434/v1" />
            </Field>
            <Field label="API key">
              <input type="password" value={cfg.llm.api_key} onChange={(e) => setLLM("api_key", e.target.value)} />
            </Field>
            <Field label="Model">
              <input value={cfg.llm.model} onChange={(e) => setLLM("model", e.target.value)} placeholder="qwen2.5:latest" />
            </Field>

            <p className="section-note">Cloud fallback (used if the local endpoint is unreachable).</p>
            <Field label="Fallback base URL">
              <input value={cfg.llm.fallback_base_url} onChange={(e) => setLLM("fallback_base_url", e.target.value)} placeholder="https://api.openai.com/v1" />
            </Field>
            <Field label="Fallback API key">
              <input type="password" value={cfg.llm.fallback_api_key} onChange={(e) => setLLM("fallback_api_key", e.target.value)} />
            </Field>
            <Field label="Fallback model">
              <input value={cfg.llm.fallback_model} onChange={(e) => setLLM("fallback_model", e.target.value)} placeholder="gpt-4o-mini" />
            </Field>
          </section>

          <section className="settings-section">
            <h3>
              <button type="button" className="advanced-toggle" onClick={() => setShowEmbeddings((v) => !v)}>
                {showEmbeddings ? "Hide" : "Show"} embeddings (optional)
              </button>
            </h3>
            {showEmbeddings && (
              <>
            <p className="section-note">Semantic memory recall. Any OpenAI-compatible embeddings endpoint.</p>
            <Field label="Embedding base URL">
              <input value={cfg.llm.embedding_base_url} onChange={(e) => setLLM("embedding_base_url", e.target.value)} />
            </Field>
            <Field label="Embedding model">
              <input value={cfg.llm.embedding_model} onChange={(e) => setLLM("embedding_model", e.target.value)} />
            </Field>
              </>
            )}
          </section>
              </>
            )}

            {section === "permissions" && (
          <section className="settings-section">
            <h3>Authorisation</h3>
            <p className="section-note">
              Most actions run immediately. I only pause for irreversible work: deleting
              or overwriting files, shutting down or rebooting, and sending email. A saved
              sudo password lets me run privileged commands without asking. Your sudo
              password stays on this machine, in ~/.jarvis/config.json.
            </p>
            <label className="toggle-row">
              <div className="toggle-copy">
                <span className="toggle-title">Auto-approve irreversible actions</span>
                <span className="toggle-desc">
                  Skip Approve/Deny even for delete, overwrite, shutdown/reboot, and email.
                </span>
              </div>
              <input
                type="checkbox"
                checked={Boolean(cfg.permissions?.auto_approve)}
                onChange={(e) => setPerm("auto_approve", e.target.checked)}
              />
            </label>
            <Field
              label={
                cfg.permissions?.sudo_configured
                  ? "Sudo password (leave masked to keep current)"
                  : "Sudo password (optional)"
              }
            >
              <input
                type="password"
                value={cfg.permissions?.sudo_password ?? ""}
                onChange={(e) => setPerm("sudo_password", e.target.value)}
                placeholder={cfg.permissions?.sudo_configured ? "••••••••" : "Enter to allow sudo commands"}
                autoComplete="new-password"
              />
            </Field>
            {cfg.permissions?.sudo_configured && (
              <button
                type="button"
                className="btn btn-ghost clear-sudo"
                onClick={() =>
                  setCfg((c) =>
                    c
                      ? {
                          ...c,
                          permissions: {
                            ...c.permissions,
                            sudo_password: "",
                            sudo_configured: false,
                          },
                        }
                      : c
                  )
                }
              >
                Clear sudo password
              </button>
            )}
            <p className="section-note warn-note">
              Granting sudo and auto-approve gives me broad control of this machine. Use only on
              a host you trust.
            </p>
          </section>
            )}

            {section === "memory" && (
              <>
          <section className="settings-section">
            <h3>REM sleep</h3>
            <p className="section-note">
              When you leave me idle, I consolidate short-term memories into long-term notes
              (~/.jarvis/MEMORY.md, DREAMS.md).
            </p>
            <label className="toggle-row">
              <div className="toggle-copy">
                <span className="toggle-title">Enable REM sleep</span>
                <span className="toggle-desc">Consolidate memory automatically when idle.</span>
              </div>
              <input
                type="checkbox"
                checked={Boolean(cfg.rem?.enabled)}
                onChange={(e) => setRem("enabled", e.target.checked)}
              />
            </label>
            <div className="grid-2">
              <Field label="Idle before REM (minutes)">
                <input
                  type="number"
                  min={1}
                  value={cfg.rem?.idle_minutes ?? 15}
                  onChange={(e) => setRem("idle_minutes", Number(e.target.value) || 15)}
                />
              </Field>
              <Field label="Min interval between cycles (minutes)">
                <input
                  type="number"
                  min={5}
                  value={cfg.rem?.min_interval_minutes ?? 60}
                  onChange={(e) => setRem("min_interval_minutes", Number(e.target.value) || 60)}
                />
              </Field>
            </div>
            {remStatus && (
              <p className="section-note">
                Phase: <strong>{remStatus.phase}</strong>
                {remStatus.last_sweep_at
                  ? ` · last cycle ${new Date(remStatus.last_sweep_at * 1000).toLocaleString()}`
                  : " · no cycle yet"}
                {remStatus.last_result ? ` — ${remStatus.last_result}` : ""}
              </p>
            )}
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => void runRemNow()}
              disabled={remRunning}
            >
              {remRunning ? "Dreaming…" : "Run REM now"}
            </button>
            {remNote && <p className="section-note">{remNote}</p>}
          </section>

          <section className="settings-section">
            <h3>Memory bank</h3>
            <p className="section-note">
              Semantic facts JARVIS recalls during chat — preferences, device notes, conversation
              summaries, and REM-consolidated long-term memory. Browse, delete individual entries,
              or wipe everything.
            </p>
            {memoryStats && (
              <p className="section-note">
                <strong>{memoryStats.total}</strong> stored memor{memoryStats.total === 1 ? "y" : "ies"}
                {memoryStats.has_memory_md ? " · MEMORY.md present" : ""}
                {memoryStats.has_dreams_md ? " · DREAMS.md present" : ""}
              </p>
            )}
            <div className="settings-row-actions">
              {onOpenMemoryBank && (
                <button type="button" className="btn btn-ghost" onClick={onOpenMemoryBank}>
                  Open memory bank
                </button>
              )}
              {!confirmWipeMemory ? (
                <button
                  type="button"
                  className="btn btn-danger-outline"
                  disabled={memoryWiping}
                  onClick={() => setConfirmWipeMemory(true)}
                >
                  Wipe all memory
                </button>
              ) : (
                <>
                  <button
                    type="button"
                    className="btn btn-danger"
                    disabled={memoryWiping}
                    onClick={() => void wipeAllMemory()}
                  >
                    {memoryWiping ? "Wiping…" : "Confirm wipe everything"}
                  </button>
                  <button
                    type="button"
                    className="btn btn-ghost"
                    disabled={memoryWiping}
                    onClick={() => setConfirmWipeMemory(false)}
                  >
                    Cancel
                  </button>
                </>
              )}
            </div>
            {confirmWipeMemory && (
              <p className="section-note warn-note">
                This permanently deletes all semantic memories and clears MEMORY.md / DREAMS.md.
                JARVIS will forget learned facts until new ones are recorded.
              </p>
            )}
            {memoryNote && <p className="section-note">{memoryNote}</p>}
          </section>
              </>
            )}

            {section === "brain" && (
          <section className="settings-section">
            <h3>Web search</h3>
            <Field label="SearXNG URL">
              <input value={cfg.searxng_url} onChange={(e) => setTop("searxng_url", e.target.value)} placeholder="http://localhost:8080" />
            </Field>
            <p className="section-note">Leave blank — I will use DuckDuckGo.</p>
          </section>
            )}

            {section === "mail" && (
          <section className="settings-section">
            <h3>Mail</h3>
            <p className="section-note">
              I send and read mail from these mailboxes. Ask for a profile by name, or I use the default.
              {onAsk ? " Prefer a walkthrough? Ask me to set this up." : ""}
            </p>
            {onAsk && (
              <button
                type="button"
                className="btn btn-ghost"
                onClick={() =>
                  onAsk("Walk me through setting up email so you can send and read messages for me.")
                }
              >
                Walk me through this
              </button>
            )}
            <div className="email-profile-bar">
              <select
                value={activeProfile.id}
                onChange={(e) => setActiveProfileId(e.target.value)}
              >
                {profiles.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                    {p.id === cfg.email_accounts.default_id ? " (default)" : ""}
                  </option>
                ))}
              </select>
              <button type="button" className="btn btn-ghost" onClick={addProfile}>
                + Add
              </button>
              <button
                type="button"
                className="btn btn-ghost"
                onClick={removeActiveProfile}
                disabled={profiles.length <= 1}
                title={profiles.length <= 1 ? "Keep at least one profile" : "Remove this profile"}
              >
                Remove
              </button>
            </div>
            <div className="grid-2">
              <Field label="Profile name">
                <input
                  value={activeProfile.name}
                  onChange={(e) => updateActiveProfile({ name: e.target.value })}
                />
              </Field>
              <Field label="Profile id">
                <input
                  value={activeProfile.id}
                  onChange={(e) => {
                    const nextId = e.target.value.trim() || activeProfile.id;
                    const oldId = activeProfile.id;
                    setCfg((c) => {
                      if (!c) return c;
                      const list = (c.email_accounts?.profiles ?? []).map((p) =>
                        p.id === oldId ? { ...p, id: nextId } : p
                      );
                      let default_id = c.email_accounts.default_id;
                      if (default_id === oldId) default_id = nextId;
                      setActiveProfileId(nextId);
                      return {
                        ...c,
                        email_accounts: { default_id, profiles: list },
                        email: list.find((p) => p.id === default_id) || list[0],
                      };
                    });
                  }}
                />
              </Field>
            </div>
            <label className="toggle-row">
              <div className="toggle-copy">
                <span className="toggle-title">Default profile</span>
                <span className="toggle-desc">Used when no profile is specified in a tool call.</span>
              </div>
              <input
                type="checkbox"
                checked={isDefault}
                onChange={(e) => {
                  if (e.target.checked) setDefaultProfile(activeProfile.id);
                }}
              />
            </label>
            <div className="grid-2">
              <Field label="SMTP host">
                <input
                  value={activeProfile.smtp_host}
                  onChange={(e) => updateActiveProfile({ smtp_host: e.target.value })}
                />
              </Field>
              <Field label="SMTP port">
                <input
                  value={activeProfile.smtp_port}
                  onChange={(e) => updateActiveProfile({ smtp_port: Number(e.target.value) || 587 })}
                />
              </Field>
              <Field label="SMTP user">
                <input
                  value={activeProfile.smtp_user}
                  onChange={(e) => updateActiveProfile({ smtp_user: e.target.value })}
                />
              </Field>
              <Field
                label={
                  activeProfile.smtp_password_configured
                    ? "SMTP password (leave blank to keep)"
                    : "SMTP password"
                }
              >
                <input
                  type="password"
                  value={activeProfile.smtp_password}
                  onChange={(e) => updateActiveProfile({ smtp_password: e.target.value })}
                  placeholder={activeProfile.smtp_password_configured ? "••••••••" : ""}
                  autoComplete="new-password"
                />
              </Field>
              <Field label="From address">
                <input
                  value={activeProfile.from_address}
                  onChange={(e) => updateActiveProfile({ from_address: e.target.value })}
                />
              </Field>
              <Field label="IMAP host">
                <input
                  value={activeProfile.imap_host}
                  onChange={(e) => updateActiveProfile({ imap_host: e.target.value })}
                />
              </Field>
              <Field label="IMAP port">
                <input
                  value={activeProfile.imap_port}
                  onChange={(e) => updateActiveProfile({ imap_port: Number(e.target.value) || 993 })}
                />
              </Field>
              <Field label="IMAP user">
                <input
                  value={activeProfile.imap_user}
                  onChange={(e) => updateActiveProfile({ imap_user: e.target.value })}
                />
              </Field>
              <Field
                label={
                  activeProfile.imap_password_configured
                    ? "IMAP password (leave blank to keep)"
                    : "IMAP password"
                }
              >
                <input
                  type="password"
                  value={activeProfile.imap_password}
                  onChange={(e) => updateActiveProfile({ imap_password: e.target.value })}
                  placeholder={activeProfile.imap_password_configured ? "••••••••" : ""}
                  autoComplete="new-password"
                />
              </Field>
            </div>
          </section>
            )}

            {section === "phone" && (
          <section className="settings-section">
            <h3>Phone</h3>
            <p className="section-note">
              Talk to me from Telegram. I long-poll outbound — this machine stays private.
              Create a bot with @BotFather, enable it here, message /whoami, then add your
              chat id to the allowlist.
            </p>
            {onAsk && (
              <button
                type="button"
                className="btn btn-ghost"
                onClick={() =>
                  onAsk("Walk me through connecting Telegram so I can talk to you from my phone. Keep it simple.")
                }
              >
                Walk me through this
              </button>
            )}
            <label className="toggle-row">
              <div className="toggle-copy">
                <span className="toggle-title">Enable Telegram bot</span>
                <span className="toggle-desc">Start long-polling when a bot token is set.</span>
              </div>
              <input
                type="checkbox"
                checked={Boolean(cfg.telegram?.enabled)}
                onChange={(e) => setTelegram("enabled", e.target.checked)}
              />
            </label>
            <Field
              label={
                cfg.telegram?.bot_token_configured
                  ? "Bot token (leave masked to keep current)"
                  : "Bot token"
              }
            >
              <input
                type="password"
                value={cfg.telegram?.bot_token ?? ""}
                onChange={(e) => setTelegram("bot_token", e.target.value)}
                placeholder={cfg.telegram?.bot_token_configured ? "••••••••" : "123456:ABC…"}
                autoComplete="new-password"
              />
            </Field>
            <Field label="Allowed chat IDs (comma-separated)">
              <input
                value={(cfg.telegram?.allowed_chat_ids ?? []).join(", ")}
                onChange={(e) =>
                  setTelegram(
                    "allowed_chat_ids",
                    e.target.value
                      .split(/[,;\s]+/)
                      .map((s) => s.trim())
                      .filter(Boolean)
                  )
                }
                placeholder="e.g. 123456789"
              />
            </Field>
            <Field label="Allowed user IDs (comma-separated)">
              <input
                value={(cfg.telegram?.allowed_user_ids ?? []).join(", ")}
                onChange={(e) =>
                  setTelegram(
                    "allowed_user_ids",
                    e.target.value
                      .split(/[,;\s]+/)
                      .map((s) => s.trim())
                      .filter(Boolean)
                  )
                }
                placeholder="optional; same as chat_id for DMs"
              />
            </Field>
            <label className="toggle-row">
              <div className="toggle-copy">
                <span className="toggle-title">Notify tool activity</span>
                <span className="toggle-desc">
                  Stream short tool call / result updates into the Telegram chat.
                </span>
              </div>
              <input
                type="checkbox"
                checked={cfg.telegram?.notify_tools ?? true}
                onChange={(e) => setTelegram("notify_tools", e.target.checked)}
              />
            </label>
            <p className="section-note warn-note">
              Anyone on the allowlist can run the same tools as the web UI (files, shell, email).
              Keep the list short. Irreversible actions still ask Approve / Deny in Telegram unless
              auto-approve is on.
            </p>
          </section>
            )}

            {section === "tools" && (
          <section className="settings-section">
            <h3>Tools</h3>
            <p className="section-note">
              Other apps can talk to me at <code>/mcp</code>. Use <strong>Connections</strong> in
              the header to browse and install servers — or just ask me to add the tools you need.
            </p>
            {onAsk && (
              <button
                type="button"
                className="btn btn-ghost"
                onClick={() =>
                  onAsk("Find useful MCP servers for me and install the ones I should have.")
                }
              >
                Find tools for me
              </button>
            )}
            <label className="toggle-row">
              <div className="toggle-copy">
                <span className="toggle-title">Enable MCP endpoint</span>
                <span className="toggle-desc">
                  Lets external MCP clients call JARVIS tools over Streamable HTTP.
                </span>
              </div>
              <input
                type="checkbox"
                checked={cfg.mcp?.enabled ?? true}
                onChange={(e) =>
                  setCfg((c) =>
                    c
                      ? {
                          ...c,
                          mcp: {
                            enabled: e.target.checked,
                            servers: c.mcp?.servers ?? [],
                          },
                        }
                      : c
                  )
                }
              />
            </label>
          </section>
            )}

            {section === "hardware" && (
          <section className="settings-section">
            <h3>Hardware</h3>
            <p className="section-note">
              I learn this machine on startup. Attached devices appear under Devices — ask me
              what's connected, or to pair headphones, dim the display, or join Wi-Fi.
            </p>
            <label className="toggle-row">
              <div className="toggle-copy">
                <span className="toggle-title">Scan peripherals</span>
                <span className="toggle-desc">
                  Inventory USB, Bluetooth, audio, displays, cameras, printers, storage,
                  and nearby networks on startup and on a short refresh interval.
                </span>
              </div>
              <input
                type="checkbox"
                checked={cfg.device?.scan_peripherals ?? true}
                onChange={(e) =>
                  setCfg((c) =>
                    c
                      ? {
                          ...c,
                          device: {
                            auto_learn: c.device?.auto_learn ?? true,
                            refresh_hours: c.device?.refresh_hours ?? 6,
                            scan_peripherals: e.target.checked,
                            peripheral_refresh_minutes:
                              c.device?.peripheral_refresh_minutes ?? 15,
                          },
                        }
                      : c
                  )
                }
              />
            </label>
            <button
              type="button"
              className="btn btn-secondary"
              disabled={deviceRefreshing}
              onClick={() => {
                setDeviceRefreshing(true);
                api
                  .refreshDevice()
                  .then(() => api.listDevices().then(setDevices))
                  .finally(() => setDeviceRefreshing(false));
              }}
            >
              {deviceRefreshing ? "Refreshing…" : "Refresh device profile"}
            </button>
            {devices.length === 0 && (
              <p className="section-note">No profile yet — start the backend or click Refresh.</p>
            )}
            {devices.map((d) => (
              <div key={d.id} className="device-row">
                <span className="device-name">{d.name}</span>
                <span className="device-meta">
                  {d.profile?.os} {d.profile?.os_release} · {d.profile?.memory_total_gb} GB RAM ·{" "}
                  {d.profile?.cpu_cores_logical} cores
                </span>
              </div>
            ))}
          </section>
            )}
          </div>
        </div>

        <div className="settings-footer">
          {saved && <span className="saved-note">Saved.</span>}
          <button className="btn btn-primary" onClick={save} disabled={saving}>
            {saving ? "Saving…" : "Save systems"}
          </button>
        </div>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
    </label>
  );
}
