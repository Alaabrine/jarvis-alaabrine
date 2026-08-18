import type { Conversation } from "../types";

interface Props {
  conversations: Conversation[];
  currentId: number | null;
  onSelect: (id: number) => void;
  onNew: () => void;
  onDelete: (id: number) => void;
  onOpenSettings: () => void;
  online: boolean;
}

export function Sidebar({
  conversations,
  currentId,
  onSelect,
  onNew,
  onDelete,
  onOpenSettings,
  online,
}: Props) {
  return (
    <aside className="sidebar">
      <div className="sidebar-header">
        <div className={`conn-dot ${online ? "conn-on" : "conn-off"}`} />
        <span>{online ? "Core online" : "Core offline"}</span>
      </div>

      <button className="new-btn" onClick={onNew}>
        <span className="plus">+</span> New session
      </button>

      <nav className="conv-list">
        {conversations.length === 0 && (
          <p className="empty-hint">No sessions yet. Start a conversation.</p>
        )}
        {conversations.map((c) => (
          <div
            key={c.id}
            className={`conv-item ${c.id === currentId ? "active" : ""}`}
            onClick={() => onSelect(c.id)}
          >
            <span className="conv-title">{c.title}</span>
            <button
              className="conv-del"
              title="Delete session"
              onClick={(e) => {
                e.stopPropagation();
                onDelete(c.id);
              }}
            >
              ×
            </button>
          </div>
        ))}
      </nav>

      <button className="settings-btn" onClick={onOpenSettings}>
        ⚙ Configuration
      </button>
    </aside>
  );
}
