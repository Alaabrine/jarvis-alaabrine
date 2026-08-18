import { useEffect, useState } from "react";
import { api } from "../api";
import type { Device, EmailProfile, JarvisConfig, RemStatus } from "../types";

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

export function SettingsModal({ onClose }: { onClose: () => void }) {
  const [cfg, setCfg] = useState<JarvisConfig | null>(null);
  const [devices, setDevices] = useState<Device[]>([]);
  const [remStatus, setRemStatus] = useState<RemStatus | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [remRunning, setRemRunning] = useState(false);
  const [remNote, setRemNote] = useState("");
  const [activeProfileId, setActiveProfileId] = useState<string>("default");

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
          telegram: {
            enabled: c.telegram?.enabled ?? false,
            bot_token: c.telegram?.bot_token === "********" ? "" : c.telegram?.bot_token || "",
            bot_token_configured: c.telegram?.bot_token_configured ?? false,
            allowed_chat_ids: c.telegram?.allowed_chat_ids ?? [],
            allowed_user_ids: c.telegram?.allowed_user_ids ?? [],
            notify_tools: c.telegram?.notify_tools ?? true,
          },
        });
        setActiveProfileId(email_accounts.default_id);
      })
      .catch(() => {});
    api.listDevices().then(setDevices).catch(() => {});
    api.remStatus().then(setRemStatus).catch(() => {});
  }, []);

  if (!cfg) {
    return (
      <div className="modal-overlay" onClick={onClose}>
        <div className="modal" onClick={(e) => e.stopPropagation()}>
          <p>Loading configuration…</p>
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
        telegram: {
          enabled: cfg.telegram?.enabled ?? false,
          bot_token: cfg.telegram?.bot_token || "********",
          allowed_chat_ids: cfg.telegram?.allowed_chat_ids ?? [],
          allowed_user_ids: cfg.telegram?.allowed_user_ids ?? [],
          notify_tools: cfg.telegram?.notify_tools ?? true,
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

  const isDefault = activeProfile.id === cfg.email_accounts.default_id;

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal settings-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h2>Configuration</h2>
          <button className="modal-close" onClick={onClose}>
            ×
          </button>
        </div>

        <div className="settings-scroll">
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
              Hold Super+` anywhere to dictate a prompt; release to send. Requires the
              desktop app. Restart the desktop shell after changing hotkeys (or set
              JARVIS_HOTKEY / JARVIS_PTT_HOTKEY).
            </p>
          </section>

          <section className="settings-section">
            <h3>Language Model</h3>
            <Field label="Prefer">
              <select value={cfg.llm.prefer} onChange={(e) => setLLM("prefer", e.target.value)}>
                <option value="local">Local (Odysseus)</option>
                <option value="cloud">Cloud API</option>
              </select>
            </Field>
            <p className="section-note">
              Primary endpoint — point this at Odysseus / Ollama / vLLM / llama.cpp.
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
            <h3>Embeddings (optional)</h3>
            <p className="section-note">Enables semantic memory recall. Any OpenAI-compatible embeddings endpoint.</p>
            <Field label="Embedding base URL">
              <input value={cfg.llm.embedding_base_url} onChange={(e) => setLLM("embedding_base_url", e.target.value)} />
            </Field>
            <Field label="Embedding model">
              <input value={cfg.llm.embedding_model} onChange={(e) => setLLM("embedding_model", e.target.value)} />
            </Field>
          </section>

          <section className="settings-section">
            <h3>Permissions</h3>
            <p className="section-note">
              Controls how JARVIS handles gated actions and privileged shell commands.
              The sudo password is stored only in your local config (~/.jarvis/config.json).
            </p>
            <label className="toggle-row">
              <div className="toggle-copy">
                <span className="toggle-title">Auto-approve gated actions</span>
                <span className="toggle-desc">
                  Skip Approve/Deny prompts for destructive tools (shell writes, delete, email, etc.).
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
              Granting sudo and auto-approve gives JARVIS broad control of this machine. Use only on
              a trusted local host.
            </p>
          </section>

          <section className="settings-section">
            <h3>REM sleep (memory consolidation)</h3>
            <p className="section-note">
              After idle time with no chat, JARVIS runs light → REM → deep cycles to promote short-term
              memories into long-term notes (~/.jarvis/MEMORY.md, DREAMS.md), similar to OpenClaw.
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
            <h3>Web Search (optional)</h3>
            <Field label="SearXNG URL">
              <input value={cfg.searxng_url} onChange={(e) => setTop("searxng_url", e.target.value)} placeholder="http://localhost:8080" />
            </Field>
            <p className="section-note">Leave blank to use the built-in DuckDuckGo fallback.</p>
          </section>

          <section className="settings-section">
            <h3>Email profiles</h3>
            <p className="section-note">
              Configure multiple mailboxes. JARVIS uses the default unless you ask for a profile by
              name or id (tools: list_email_profiles, send_email, read_email).
            </p>
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

          <section className="settings-section">
            <h3>Telegram</h3>
            <p className="section-note">
              Chat with JARVIS from your phone. The bot long-polls Telegram outbound — your API
              stays on localhost. Create a bot with @BotFather, enable it here, then message the bot
              /whoami and add your chat_id or user_id to the allowlist.
            </p>
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
              Keep the list short. Destructive actions still ask Approve / Deny in Telegram unless
              auto-approve is on.
            </p>
          </section>

          <section className="settings-section">
            <h3>Known Devices ({devices.length})</h3>
            {devices.length === 0 && <p className="section-note">Ask JARVIS to "scan this device" to register it.</p>}
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
        </div>

        <div className="settings-footer">
          {saved && <span className="saved-note">Saved.</span>}
          <button className="btn btn-primary" onClick={save} disabled={saving}>
            {saving ? "Saving…" : "Save configuration"}
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
