import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import type { McpCatalogEntry, McpServerEntry, McpServerStatus, McpStatus } from "../types";
import { McpDetailModal } from "./McpDetailModal";
import { McpIcon } from "./McpIcon";

function iconFromMcpUrl(url: string): string {
  try {
    const host = new URL(url).hostname;
    if (host) return `https://icons.duckduckgo.com/ip3/${host}.ico`;
  } catch {
    /* ignore invalid URLs */
  }
  return "";
}

function serverConnectionLabel(entry: McpServerStatus | undefined, enabled: boolean): string {
  if (!enabled) return "disabled";
  if (!entry) return "unknown";
  if (entry.connected) return "connected";
  return entry.error ? "error" : "offline";
}

function emptyServer(): McpServerEntry {
  return {
    id: `server-${Date.now().toString(36)}`,
    name: "MCP server",
    url: "",
    enabled: true,
    headers: {},
  };
}

function normalizeServers(servers: McpServerEntry[] | undefined): McpServerEntry[] {
  return (servers ?? []).map((s) => ({
    ...emptyServer(),
    ...s,
    headers: { ...(s.headers ?? {}) },
    enabled: s.enabled !== false,
  }));
}

export function McpView({
  onBack,
  onSaved,
}: {
  onBack: () => void;
  onSaved?: () => void;
}) {
  const [status, setStatus] = useState<McpStatus | null>(null);
  const [servers, setServers] = useState<McpServerEntry[]>([]);
  const [mcpEnabled, setMcpEnabled] = useState(true);
  const [activeId, setActiveId] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [testing, setTesting] = useState(false);
  const [note, setNote] = useState("");
  const [copied, setCopied] = useState("");
  const [catalogQuery, setCatalogQuery] = useState("");
  const [catalogItems, setCatalogItems] = useState<McpCatalogEntry[]>([]);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [installingId, setInstallingId] = useState<string | null>(null);
  const [authForId, setAuthForId] = useState<string | null>(null);
  const [authToken, setAuthToken] = useState("");
  const [detailEntry, setDetailEntry] = useState<McpCatalogEntry | null>(null);
  const [removing, setRemoving] = useState(false);
  const remotePanelRef = useRef<HTMLDivElement>(null);

  async function load() {
    setLoading(true);
    try {
      const [st, cfg] = await Promise.all([api.mcpStatus(), api.getConfig()]);
      setStatus(st);
      const list = normalizeServers(cfg.mcp?.servers).map((s) => {
        const raw = cfg.mcp?.servers?.find((x) => x.id === s.id);
        return {
          ...s,
          headers_configured: raw?.headers_configured,
        };
      });
      setServers(list);
      setMcpEnabled(cfg.mcp?.enabled ?? true);
      setActiveId((prev) =>
        list.some((s) => s.id === prev) ? prev : list[0]?.id ?? ""
      );
    } catch (err) {
      setNote(err instanceof Error ? err.message : "Failed to load MCP status");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    void searchCatalog("");
  }, []);

  async function searchCatalog(query: string) {
    setCatalogLoading(true);
    try {
      const res = await api.mcpCatalogSearch(query, 24);
      setCatalogItems(res.items);
    } catch (err) {
      setNote(err instanceof Error ? err.message : "Catalog search failed");
    } finally {
      setCatalogLoading(false);
    }
  }

  async function installCatalog(entry: McpCatalogEntry, authorization?: string) {
    if (!entry.url && !entry.profile_url) {
      setNote("This listing has no installable remote URL.");
      return;
    }
    setInstallingId(entry.id);
    setNote("");
    try {
      const result = await api.mcpCatalogInstall({
        catalog_id: entry.id,
        url: entry.url || undefined,
        authorization: authorization || undefined,
        name: entry.title,
      });
      await load();
      onSaved?.();
      if (result.already_installed) {
        setNote(`Already installed: ${result.name}`);
      } else if (result.connected) {
        setNote(`Installed ${result.name} — ${result.tool_count ?? 0} tool(s) integrated.`);
      } else {
        setNote(
          `Installed ${result.name}, but connection failed: ${result.error || "unknown error"}`
        );
      }
      setAuthForId(null);
      setAuthToken("");
      await searchCatalog(catalogQuery);
    } catch (err) {
      setNote(err instanceof Error ? err.message : "Install failed");
    } finally {
      setInstallingId(null);
    }
  }

  function requestInstall(entry: McpCatalogEntry) {
    if (entry.requires_auth && !entry.installed) {
      setAuthForId(entry.id);
      setAuthToken("");
      return;
    }
    void installCatalog(entry);
  }

  const active =
    servers.find((s) => s.id === activeId) || servers[0] || emptyServer();
  const activeStatus = status?.servers.find((s) => s.id === active.id);

  const importedCount = status?.imported_tool_count ?? 0;
  const connectedCount = status?.servers.filter((s) => s.connected).length ?? 0;

  const installedServers = useMemo(
    () =>
      servers
        .filter((s) => s.url.trim() || (s.command ?? "").trim())
        .map((config) => ({
          config,
          live: status?.servers.find((s) => s.id === config.id),
        })),
    [servers, status]
  );

  function focusServer(id: string) {
    setActiveId(id);
    remotePanelRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  const cursorSnippet = useMemo(
    () =>
      JSON.stringify(
        {
          mcpServers: {
            jarvis: { url: status?.endpoint || "http://127.0.0.1:8787/mcp" },
          },
        },
        null,
        2
      ),
    [status?.endpoint]
  );

  function updateActive(patch: Partial<McpServerEntry>) {
    setServers((list) =>
      list.map((s) => (s.id === active.id ? { ...s, ...patch } : s))
    );
  }

  function updateHeader(key: string, value: string) {
    setServers((list) =>
      list.map((s) =>
        s.id === active.id
          ? { ...s, headers: { ...(s.headers ?? {}), [key]: value } }
          : s
      )
    );
  }

  function addServer() {
    const entry = emptyServer();
    setServers((list) => [...list, entry]);
    setActiveId(entry.id);
  }

  function serializeServer(s: McpServerEntry) {
    const headers: Record<string, string> = {};
    for (const [key, value] of Object.entries(s.headers ?? {})) {
      const k = key.trim();
      if (!k) continue;
      const v = (value || "").trim();
      if (v) {
        headers[k] = v;
      } else if (s.headers_configured && k.toLowerCase() === "authorization") {
        headers[k] = "********";
      }
    }
    const env: Record<string, string> = {};
    for (const [key, value] of Object.entries(s.env ?? {})) {
      const k = key.trim();
      if (!k) continue;
      const v = (value || "").trim();
      if (v) {
        env[k] = v;
      } else if (s.env_configured) {
        env[k] = "********";
      }
    }
    const command = (s.command ?? "").trim();
    return {
      id: s.id,
      name: s.name,
      url: s.url,
      enabled: s.enabled,
      headers,
      // Local (stdio) servers JARVIS provisioned itself must round-trip intact.
      transport: s.transport ?? (command ? "stdio" : "http"),
      command,
      args: s.args ?? [],
      env,
      cwd: s.cwd ?? "",
    };
  }

  async function persistServers(
    list: McpServerEntry[],
    { refreshAfter = true }: { refreshAfter?: boolean } = {}
  ) {
    const toSave = list.filter((s) => s.url.trim() || (s.command ?? "").trim());
    await api.saveConfig({
      mcp: {
        enabled: mcpEnabled,
        servers: toSave.map(serializeServer),
      },
    });
    if (refreshAfter) {
      await api.mcpRefresh();
    }
    await load();
  }

  async function removeActive() {
    const next = servers.filter((s) => s.id !== active.id);
    setRemoving(true);
    setNote("");
    try {
      await persistServers(next);
      onSaved?.();
      setNote("Server removed.");
    } catch (err) {
      setNote(err instanceof Error ? err.message : "Remove failed");
    } finally {
      setRemoving(false);
    }
  }

  async function saveServers(refreshAfter = true) {
    setSaving(true);
    setNote("");
    try {
      await persistServers(servers, { refreshAfter });
      onSaved?.();
      setNote("Saved.");
    } catch (err) {
      setNote(err instanceof Error ? err.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  async function testActive() {
    setTesting(true);
    setNote("");
    try {
      const result = await api.mcpTestServer(active);
      if (result.ok) {
        setNote(`Connected — ${result.tools.length} tool(s) found.`);
      } else {
        setNote(result.error || "Connection failed");
      }
    } catch (err) {
      setNote(err instanceof Error ? err.message : "Test failed");
    } finally {
      setTesting(false);
    }
  }

  async function refreshTools() {
    setRefreshing(true);
    setNote("");
    try {
      const res = await api.mcpRefresh();
      setNote(`Refreshed — ${res.imported_tool_count ?? 0} imported tool(s).`);
      await load();
    } catch (err) {
      setNote(err instanceof Error ? err.message : "Refresh failed");
    } finally {
      setRefreshing(false);
    }
  }

  async function copy(text: string, label: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(label);
      window.setTimeout(() => setCopied(""), 2000);
    } catch {
      setNote("Could not copy to clipboard");
    }
  }

  const authHeader = active.headers?.Authorization ?? active.headers?.authorization ?? "";

  return (
    <section className="tasks-view mcp-view">
      <div className="tasks-view-head">
        <div className="tasks-view-title">
          <h2>Connections</h2>
          <span className="tasks-view-sub">
            Extra tools I can use, and the endpoint other apps use to talk to me. Ask me
            to find and install a server if you would rather not browse.
          </span>
        </div>
        <div className="tasks-view-stats">
          <span className="tasks-stat">
            <strong>{importedCount}</strong> imported
          </span>
          <span className="tasks-stat">
            <strong>{connectedCount}</strong> online
          </span>
        </div>
        <button type="button" className="btn btn-ghost" onClick={onBack}>
          Close
        </button>
      </div>

      {loading ? (
        <p className="section-note mcp-note">Loading MCP configuration…</p>
      ) : (
        <div className="tasks-view-body mcp-body">
          <div className="mcp-panel">
            <h3>JARVIS MCP server</h3>
            <p className="section-note">
              Cursor, Claude Desktop, and other MCP clients can connect here.
            </p>
            <label className="toggle-row">
              <div className="toggle-copy">
                <span className="toggle-title">Enable MCP endpoint</span>
                <span className="toggle-desc">Streamable HTTP at /mcp (restart required if toggled off at startup).</span>
              </div>
              <input
                type="checkbox"
                checked={mcpEnabled}
                onChange={(e) => setMcpEnabled(e.target.checked)}
              />
            </label>
            <div className="mcp-endpoint-row">
              <code className="mcp-endpoint">{status?.endpoint || "http://127.0.0.1:8787/mcp"}</code>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={() => void copy(status?.endpoint || "", "url")}
              >
                {copied === "url" ? "Copied" : "Copy URL"}
              </button>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={() => void copy(cursorSnippet, "cursor")}
              >
                {copied === "cursor" ? "Copied" : "Copy Cursor config"}
              </button>
            </div>
            {status && status.exposed_tools.length > 0 && (
              <details className="mcp-tools-details">
                <summary>{status.exposed_tools.length} built-in tools exposed</summary>
                <ul className="mcp-tool-list">
                  {status.exposed_tools.map((t) => (
                    <li key={t.name}>
                      <strong>{t.name}</strong>
                      {t.dangerous && <span className="mcp-badge warn">gated</span>}
                      <span>{t.description.slice(0, 120)}…</span>
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </div>

          <div className="mcp-panel">
            <div className="mcp-panel-head">
              <h3>Installed MCP servers</h3>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={() => void refreshTools()}
                disabled={refreshing}
              >
                {refreshing ? "Refreshing…" : "Refresh all"}
              </button>
            </div>
            <p className="section-note">
              Servers configured in JARVIS — remote endpoints, plus local ones JARVIS
              installed for itself and runs as a subprocess. Their tools are available to
              the agent as{" "}
              <code>mcp_&lt;server&gt;_&lt;tool&gt;</code> (or via the <code>mcp_invoke</code>{" "}
              gateway).
            </p>
            {installedServers.length === 0 ? (
              <p className="mcp-catalog-empty">
                No servers installed yet — discover one above or add manually below.
              </p>
            ) : (
              <ul className="mcp-installed-list">
                {installedServers.map(({ config, live }) => {
                  const conn = serverConnectionLabel(live, config.enabled);
                  const toolCount = live?.tools.length ?? 0;
                  return (
                    <li key={config.id} className={`mcp-installed-item mcp-installed-${conn}`}>
                      <McpIcon
                        title={config.name}
                        name={config.name}
                        iconUrl={iconFromMcpUrl(config.url)}
                        size={44}
                      />
                      <div className="mcp-installed-main">
                        <div className="mcp-catalog-title-row">
                          <span
                            className={`peri-dot peri-dot-${conn === "connected" ? "connected" : "offline"}`}
                            title={conn}
                          />
                          <strong>{config.name || config.url || config.command}</strong>
                          <span className={`mcp-badge mcp-installed-badge-${conn}`}>{conn}</span>
                          {toolCount > 0 && (
                            <span className="mcp-badge muted">{toolCount} tools</span>
                          )}
                          {!config.url && (config.command ?? "").trim() && (
                            <span className="mcp-badge muted">local</span>
                          )}
                        </div>
                        <code className="mcp-installed-url">
                          {config.url ||
                            [config.command, ...(config.args ?? [])].filter(Boolean).join(" ")}
                        </code>
                        {conn === "error" && live?.error && (
                          <p className="section-note warn-note mcp-installed-error-msg">{live.error}</p>
                        )}
                        {live && live.tools.length > 0 && (
                          <details className="mcp-installed-tools">
                            <summary>Imported tools</summary>
                            <ul className="mcp-tool-list compact">
                              {live.tools.map((t) => (
                                <li key={t.proxy_name}>
                                  <strong>{t.name}</strong>
                                  <code>{t.proxy_name}</code>
                                  {t.destructive && (
                                    <span className="mcp-badge warn">destructive</span>
                                  )}
                                </li>
                              ))}
                            </ul>
                          </details>
                        )}
                      </div>
                      <div className="mcp-installed-actions">
                        <button
                          type="button"
                          className="btn btn-ghost btn-sm"
                          onClick={() => focusServer(config.id)}
                        >
                          Configure
                        </button>
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>

          <div className="mcp-panel">
            <div className="mcp-panel-head">
              <h3>Discover MCP servers</h3>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={() => void searchCatalog(catalogQuery)}
                disabled={catalogLoading}
              >
                {catalogLoading ? "Searching…" : "Refresh catalog"}
              </button>
            </div>
            <p className="section-note">
              JARVIS searches the official MCP registry, Smithery, and MCP.so listings. Click
              Install to add a remote server and import its tools automatically.
            </p>
            <div className="mcp-search-bar">
              <span className="mcp-search-icon" aria-hidden>
                ⌕
              </span>
              <input
                className="mcp-search-input"
                value={catalogQuery}
                onChange={(e) => setCatalogQuery(e.target.value)}
                placeholder="Search registries — fetch, github, postgres, browser…"
                onKeyDown={(e) => {
                  if (e.key === "Enter") void searchCatalog(catalogQuery);
                }}
              />
              <button
                type="button"
                className="btn btn-primary btn-sm mcp-search-btn"
                onClick={() => void searchCatalog(catalogQuery)}
                disabled={catalogLoading}
              >
                {catalogLoading ? "…" : "Search"}
              </button>
            </div>
            <ul className="mcp-catalog-list">
              {catalogLoading && catalogItems.length === 0 && (
                <li className="mcp-catalog-empty">Searching directories…</li>
              )}
              {!catalogLoading && catalogItems.length === 0 && (
                <li className="mcp-catalog-empty">No results — try another keyword.</li>
              )}
              {catalogItems.map((item) => (
                <li key={item.id} className="mcp-catalog-item">
                  <div
                    className="mcp-catalog-card"
                    role="button"
                    tabIndex={0}
                    onClick={() => setDetailEntry(item)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        setDetailEntry(item);
                      }
                    }}
                  >
                    <McpIcon
                      title={item.title}
                      name={item.name}
                      iconUrl={item.icon_url}
                      size={48}
                    />
                    <div className="mcp-catalog-main">
                      <div className="mcp-catalog-title-row">
                        <strong>{item.title}</strong>
                        <span className="mcp-badge">{item.source}</span>
                        {item.installed && (
                          <span className="mcp-badge installed">installed</span>
                        )}
                        {item.requires_auth && <span className="mcp-badge warn">auth</span>}
                        {!item.url && <span className="mcp-badge warn">no URL</span>}
                      </div>
                      <p className="mcp-catalog-desc">
                        {(item.description || "").slice(0, 160)}
                        {(item.description || "").length > 160 ? "…" : ""}
                      </p>
                      {item.tool_hints?.length > 0 && (
                        <span className="mcp-catalog-tools">
                          {item.tool_count || item.tool_hints.length} tools ·{" "}
                          {item.tool_hints.slice(0, 3).join(", ")}
                          {item.tool_hints.length > 3 ? "…" : ""}
                        </span>
                      )}
                    </div>
                  </div>
                  <div className="mcp-catalog-actions">
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm"
                      onClick={() => setDetailEntry(item)}
                    >
                      Details
                    </button>
                    <button
                      type="button"
                      className="btn btn-primary btn-sm"
                      disabled={!item.url || item.installed || installingId === item.id}
                      onClick={() => requestInstall(item)}
                    >
                      {installingId === item.id
                        ? "Installing…"
                        : item.installed
                          ? "Installed"
                          : "Install"}
                    </button>
                  </div>
                  {authForId === item.id && (
                    <div className="mcp-auth-row">
                      <input
                        type="password"
                        value={authToken}
                        onChange={(e) => setAuthToken(e.target.value)}
                        placeholder={item.auth_hint || "Bearer token (optional)"}
                        autoComplete="new-password"
                      />
                      <button
                        type="button"
                        className="btn btn-primary btn-sm"
                        onClick={() => void installCatalog(item, authToken)}
                        disabled={installingId === item.id}
                      >
                        Confirm install
                      </button>
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm"
                        onClick={() => setAuthForId(null)}
                      >
                        Cancel
                      </button>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          </div>

          <div className="mcp-panel" ref={remotePanelRef}>
            <div className="mcp-panel-head">
              <h3>Remote MCP servers</h3>
              <button type="button" className="btn btn-secondary btn-sm" onClick={addServer}>
                + Add server
              </button>
            </div>
            <p className="section-note">
              Tools from these servers are imported into JARVIS (prefixed with <code>mcp_</code>).
            </p>

            {servers.length === 0 ? (
              <p className="section-note">No servers yet — add one to import external tools.</p>
            ) : (
              <>
                <div className="email-profile-bar">
                  <select value={active.id} onChange={(e) => setActiveId(e.target.value)}>
                    {servers.map((s) => (
                      <option key={s.id} value={s.id}>
                        {s.name || s.url || s.id}
                        {!s.enabled ? " (disabled)" : ""}
                      </option>
                    ))}
                  </select>
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    onClick={() => void removeActive()}
                    disabled={
                      removing || (servers.length <= 1 && !servers[0]?.url.trim())
                    }
                  >
                    {removing ? "Removing…" : "Remove"}
                  </button>
                </div>

                <div className="grid-2">
                  <label className="field">
                    <span className="field-label">Name</span>
                    <input
                      value={active.name}
                      onChange={(e) => updateActive({ name: e.target.value })}
                    />
                  </label>
                  <label className="field">
                    <span className="field-label">Server URL</span>
                    <input
                      value={active.url}
                      onChange={(e) => updateActive({ url: e.target.value })}
                      placeholder="http://127.0.0.1:3000/mcp"
                    />
                  </label>
                </div>

                <label className="toggle-row">
                  <div className="toggle-copy">
                    <span className="toggle-title">Enabled</span>
                    <span className="toggle-desc">Import tools from this server into JARVIS.</span>
                  </div>
                  <input
                    type="checkbox"
                    checked={active.enabled}
                    onChange={(e) => updateActive({ enabled: e.target.checked })}
                  />
                </label>

                <label className="field">
                  <span className="field-label">
                    Authorization header{" "}
                    {active.headers_configured ? "(leave masked to keep)" : "(optional)"}
                  </span>
                  <input
                    type="password"
                    value={authHeader}
                    onChange={(e) => updateHeader("Authorization", e.target.value)}
                    placeholder={active.headers_configured ? "••••••••" : "Bearer …"}
                    autoComplete="new-password"
                  />
                </label>

                {activeStatus && (
                  <p className={`section-note ${activeStatus.connected ? "" : "warn-note"}`}>
                    {activeStatus.connected
                      ? `Connected · ${activeStatus.tools.length} tool(s) imported`
                      : activeStatus.error || "Not connected — save and refresh, or test connection."}
                  </p>
                )}

                {activeStatus?.tools && activeStatus.tools.length > 0 && (
                  <ul className="mcp-tool-list compact">
                    {activeStatus.tools.map((t) => (
                      <li key={t.proxy_name}>
                        <strong>{t.name}</strong>
                        <code>{t.proxy_name}</code>
                        {t.destructive && <span className="mcp-badge warn">destructive</span>}
                      </li>
                    ))}
                  </ul>
                )}

                <div className="mcp-actions">
                  <button
                    type="button"
                    className="btn btn-ghost"
                    onClick={() => void testActive()}
                    disabled={testing || !active.url.trim()}
                  >
                    {testing ? "Testing…" : "Test connection"}
                  </button>
                  <button
                    type="button"
                    className="btn btn-ghost"
                    onClick={() => void refreshTools()}
                    disabled={refreshing}
                  >
                    {refreshing ? "Refreshing…" : "Refresh tools"}
                  </button>
                  <button
                    type="button"
                    className="btn btn-primary"
                    onClick={() => void saveServers()}
                    disabled={saving}
                  >
                    {saving ? "Saving…" : "Save servers"}
                  </button>
                </div>
              </>
            )}
          </div>

          {note && <p className="section-note mcp-note">{note}</p>}
        </div>
      )}

      {detailEntry && (
        <McpDetailModal
          entry={detailEntry}
          onClose={() => setDetailEntry(null)}
          onInstall={(entry) => {
            setDetailEntry(null);
            requestInstall(entry);
          }}
          installing={installingId === detailEntry.id}
        />
      )}
    </section>
  );
}
