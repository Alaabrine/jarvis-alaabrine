import { useMemo, useState } from "react";

function initials(title: string, name: string): string {
  const src = (title || name || "?").trim();
  const parts = src.split(/[\s./_-]+/).filter(Boolean);
  if (parts.length >= 2) {
    return (parts[0][0] + parts[1][0]).toUpperCase();
  }
  return src.slice(0, 2).toUpperCase();
}

export function McpIcon({
  title,
  name,
  iconUrl,
  size = 44,
  className = "",
}: {
  title: string;
  name: string;
  iconUrl?: string;
  size?: number;
  className?: string;
}) {
  const [failed, setFailed] = useState(false);
  const label = useMemo(() => initials(title, name), [title, name]);
  const showImage = Boolean(iconUrl) && !failed;

  return (
    <div
      className={`mcp-icon ${className}`.trim()}
      style={{ width: size, height: size }}
      aria-hidden
    >
      {showImage ? (
        <img
          src={iconUrl}
          alt=""
          loading="lazy"
          decoding="async"
          referrerPolicy="no-referrer"
          onError={() => setFailed(true)}
        />
      ) : (
        <span className="mcp-icon-fallback">{label}</span>
      )}
    </div>
  );
}
