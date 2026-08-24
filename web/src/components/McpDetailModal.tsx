import { useEffect, useState } from "react";
import { api } from "../api";
import type { McpCatalogDetail, McpCatalogEntry } from "../types";
import { McpIcon } from "./McpIcon";

export function McpDetailModal({
  entry,
  onClose,
  onInstall,
  installing,
}: {
  entry: McpCatalogEntry;
  onClose: () => void;
  onInstall: (entry: McpCatalogEntry | McpCatalogDetail) => void;
  installing: boolean;
}) {
  const [detail, setDetail] = useState<McpCatalogDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    api
      .mcpCatalogDetail(entry.id)
      .then((d) => {
        if (!cancelled) setDetail(d);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load details");
          setDetail({ ...entry, tools_detail: [] });
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [entry]);

  const data = detail ?? entry;
  const tools = detail?.tools_detail ?? [];

  return (
    <div className="modal-overlay mcp-detail-overlay" onClick={onClose}>
      <div className="modal mcp-detail-modal" onClick={(e) => e.stopPropagation()}>
        <div className="mcp-detail-head">
          <McpIcon
            title={data.title}
            name={data.name}
            iconUrl={data.icon_url}
            size={56}
            className="mcp-icon-lg"
          />
          <div className="mcp-detail-head-copy">
            <div className="mcp-catalog-title-row">
              <h2>{data.title}</h2>
              <span className="mcp-badge">{data.source}</span>
              {data.version && <span className="mcp-badge muted">v{data.version}</span>}
              {data.installed && <span className="mcp-badge installed">installed</span>}
            </div>
            <p className="mcp-detail-sub">{data.name}</p>
          </div>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>

        <div className="mcp-detail-body">
          {loading && <p className="section-note">Loading server details…</p>}
          {error && <p className="section-note warn-note">{error}</p>}

          <p className="mcp-detail-desc">{data.description || "No description provided."}</p>

          <div className="mcp-detail-meta">
            {data.url && (
              <div className="mcp-detail-meta-row">
                <span className="mcp-detail-label">MCP endpoint</span>
                <code>{data.url}</code>
              </div>
            )}
            {data.website_url && (
              <div className="mcp-detail-meta-row">
                <span className="mcp-detail-label">Website</span>
                <a href={data.website_url} target="_blank" rel="noreferrer">
                  {data.website_url}
                </a>
              </div>
            )}
            {data.repository && (
              <div className="mcp-detail-meta-row">
                <span className="mcp-detail-label">Repository</span>
                <a href={data.repository} target="_blank" rel="noreferrer">
                  {data.repository}
                </a>
              </div>
            )}
            {data.requires_auth && (
              <div className="mcp-detail-meta-row">
                <span className="mcp-detail-label">Auth</span>
                <span className="warn-note">{data.auth_hint || "Authorization required"}</span>
              </div>
            )}
          </div>

          {data.tags?.length > 0 && (
            <div className="mcp-detail-tags">
              {data.tags.map((tag) => (
                <span key={tag} className="mcp-badge muted">
                  {tag}
                </span>
              ))}
            </div>
          )}

          {tools.length > 0 && (
            <section className="mcp-detail-tools">
              <h3>
                Tools <span className="mcp-detail-count">{tools.length}</span>
              </h3>
              <ul className="mcp-detail-tool-grid">
                {tools.map((tool) => (
                  <li key={tool.name}>
                    <strong>{tool.name}</strong>
                    <p>{tool.description || "No description."}</p>
                  </li>
                ))}
              </ul>
            </section>
          )}

          {!loading && tools.length === 0 && data.url && (
            <p className="section-note">
              Tool list unavailable — connect after install to discover tools.
            </p>
          )}
        </div>

        <div className="mcp-detail-footer">
          {data.profile_url && (
            <a
              className="btn btn-ghost"
              href={data.profile_url}
              target="_blank"
              rel="noreferrer"
            >
              Registry page
            </a>
          )}
          <button type="button" className="btn btn-ghost" onClick={onClose}>
            Close
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={!data.url || data.installed || installing}
            onClick={() => onInstall(data)}
          >
            {installing ? "Installing…" : data.installed ? "Installed" : "Install to JARVIS"}
          </button>
        </div>
      </div>
    </div>
  );
}
