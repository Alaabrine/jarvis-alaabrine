import type { AgentState } from "../types";

export function StatusOrb({
  state,
  online,
  remPhase,
}: {
  state: AgentState;
  online: boolean;
  remPhase?: string | null;
}) {
  const cls = !online ? "offline" : remPhase ? `rem-${remPhase}` : state;
  return (
    <div className={`orb orb-${cls}`}>
      <div className="orb-core" />
      <div className="orb-ring" />
      <div className="orb-ring orb-ring-2" />
    </div>
  );
}
