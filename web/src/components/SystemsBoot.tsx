import { useEffect, useState } from "react";
import { api } from "../api";
import { StatusOrb } from "./StatusOrb";

export const BOOT_KEY = "jarvis.boot.v1";

function timeGreeting(): string {
  const h = new Date().getHours();
  if (h < 5) return "Working late";
  if (h < 12) return "Good morning";
  if (h < 18) return "Good afternoon";
  return "Good evening";
}

export function SystemsBoot({
  onDone,
  speak,
}: {
  onDone: () => void;
  speak?: (text: string) => void;
}) {
  const [step, setStep] = useState(0);
  const [name, setName] = useState("");
  const [mind, setMind] = useState<"local" | "cloud">("local");
  const [model, setModel] = useState("qwen2.5:latest");
  const [baseUrl, setBaseUrl] = useState("http://localhost:11434/v1");
  const [apiKey, setApiKey] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .getConfig()
      .then((c) => {
        if (c.user_name && c.user_name !== "Sir") setName(c.user_name);
        if (c.llm?.prefer === "cloud") setMind("cloud");
        if (c.llm?.prefer === "cloud") {
          if (c.llm.fallback_model) setModel(c.llm.fallback_model);
          if (c.llm.fallback_base_url) setBaseUrl(c.llm.fallback_base_url);
        } else {
          if (c.llm?.model) setModel(c.llm.model);
          if (c.llm?.base_url) setBaseUrl(c.llm.base_url);
        }
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    const lines = [
      `${timeGreeting()}. JARVIS coming online.`,
      "How shall I address you?",
      "Where would you like me to think — locally, or in the cloud?",
      "All systems standing by. Speak when you are ready.",
    ];
    const line = lines[step];
    if (line) speak?.(line);
    // Speak once per step; speak identity is stable for the overlay lifetime.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step]);

  function finishWithoutSave() {
    try {
      localStorage.setItem(BOOT_KEY, "1");
    } catch {
      /* ignore */
    }
    onDone();
  }

  async function finish() {
    setSaving(true);
    setError("");
    try {
      const cfg = await api.getConfig();
      const address = name.trim() || "Sir";
      const payload: Record<string, unknown> = {
        user_name: address,
        llm: {
          ...cfg.llm,
          prefer: mind,
        },
      };
      const llm = payload.llm as Record<string, unknown>;
      if (mind === "local") {
        llm.base_url = baseUrl.trim() || "http://localhost:11434/v1";
        llm.model = model.trim() || "qwen2.5:latest";
        if (apiKey.trim()) llm.api_key = apiKey.trim();
      } else {
        llm.fallback_base_url = baseUrl.trim() || "https://api.openai.com/v1";
        llm.fallback_model = model.trim() || "gpt-4o-mini";
        if (apiKey.trim()) llm.fallback_api_key = apiKey.trim();
      }
      await api.saveConfig(payload);
      try {
        localStorage.setItem(BOOT_KEY, "1");
      } catch {
        /* ignore */
      }
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save systems");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="boot-overlay">
      <div className="boot-panel">
        <StatusOrb state="idle" online />
        {step === 0 && (
          <>
            <p className="boot-kicker">Just A Rather Very Intelligent System</p>
            <h2 className="boot-title">{timeGreeting()}.</h2>
            <p className="boot-copy">
              JARVIS coming online. I run on this machine — I will learn your hardware,
              listen when you speak, and act on what you ask. A moment to introduce ourselves.
            </p>
            <div className="boot-actions">
              <button type="button" className="btn btn-ghost" onClick={finishWithoutSave}>
                Skip
              </button>
              <button type="button" className="btn btn-primary" onClick={() => setStep(1)}>
                Continue
              </button>
            </div>
          </>
        )}
        {step === 1 && (
          <>
            <p className="boot-kicker">Identity</p>
            <h2 className="boot-title">How shall I address you?</h2>
            <p className="boot-copy">Sir is traditional. Use whatever you prefer.</p>
            <label className="field">
              <span className="field-label">Address me as</span>
              <input
                autoFocus
                value={name}
                placeholder="Sir"
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") setStep(2);
                }}
              />
            </label>
            <div className="boot-actions">
              <button type="button" className="btn btn-ghost" onClick={() => setStep(0)}>
                Back
              </button>
              <button type="button" className="btn btn-primary" onClick={() => setStep(2)}>
                Continue
              </button>
            </div>
          </>
        )}
        {step === 2 && (
          <>
            <p className="boot-kicker">Mind</p>
            <h2 className="boot-title">Where shall I think?</h2>
            <p className="boot-copy">
              Local is how Tony would run me — Odysseus, Ollama, or any OpenAI-compatible
              endpoint on this machine. Cloud is a fallback if you prefer an API.
            </p>
            <div className="boot-minds">
              <button
                type="button"
                className={`boot-mind ${mind === "local" ? "on" : ""}`}
                onClick={() => {
                  setMind("local");
                  setBaseUrl("http://localhost:11434/v1");
                  setModel("qwen2.5:latest");
                  setApiKey("");
                }}
              >
                <strong>This machine</strong>
                <span>Odysseus / Ollama / vLLM</span>
              </button>
              <button
                type="button"
                className={`boot-mind ${mind === "cloud" ? "on" : ""}`}
                onClick={() => {
                  setMind("cloud");
                  setBaseUrl("https://api.openai.com/v1");
                  setModel("gpt-4o-mini");
                  setApiKey("");
                }}
              >
                <strong>Cloud API</strong>
                <span>OpenAI, OpenRouter, …</span>
              </button>
            </div>
            <label className="field">
              <span className="field-label">{mind === "local" ? "Endpoint" : "API base URL"}</span>
              <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} />
            </label>
            <label className="field">
              <span className="field-label">Model</span>
              <input value={model} onChange={(e) => setModel(e.target.value)} />
            </label>
            {mind === "cloud" && (
              <label className="field">
                <span className="field-label">API key</span>
                <input
                  type="password"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder="sk-…"
                  autoComplete="new-password"
                />
              </label>
            )}
            <div className="boot-actions">
              <button type="button" className="btn btn-ghost" onClick={() => setStep(1)}>
                Back
              </button>
              <button type="button" className="btn btn-primary" onClick={() => setStep(3)}>
                Continue
              </button>
            </div>
          </>
        )}
        {step === 3 && (
          <>
            <p className="boot-kicker">Standing by</p>
            <h2 className="boot-title">I am ready, {name.trim() || "Sir"}.</h2>
            <p className="boot-copy">
              Hold Super+` or the microphone and speak. Mail, Telegram, extra tools,
              and devices — just ask. You should not have to hunt through menus.
            </p>
            {error && <p className="boot-error">{error}</p>}
            <div className="boot-actions">
              <button type="button" className="btn btn-ghost" onClick={() => setStep(2)}>
                Back
              </button>
              <button
                type="button"
                className="btn btn-primary"
                disabled={saving}
                onClick={() => void finish()}
              >
                {saving ? "Initialising…" : "Bring systems online"}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
