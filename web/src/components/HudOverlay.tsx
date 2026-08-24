import type { ReactNode } from "react";

/** Holographic system panel over the live console — chat stays mounted underneath. */
export function HudOverlay({
  children,
  onClose,
}: {
  children: ReactNode;
  onClose: () => void;
}) {
  return (
    <div
      className="hud-overlay"
      role="dialog"
      aria-modal="true"
      onClick={onClose}
    >
      <div className="hud-panel" onClick={(e) => e.stopPropagation()}>
        {children}
      </div>
    </div>
  );
}
