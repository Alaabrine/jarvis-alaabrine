import { useEffect } from "react";
import type { ConfirmationRequest } from "../types";

interface Props {
  request: ConfirmationRequest;
  onAnswer: (id: string, approved: boolean) => void;
}

export function ConfirmModal({ request, onAnswer }: Props) {
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Enter") {
        e.preventDefault();
        onAnswer(request.id, true);
      } else if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        onAnswer(request.id, false);
      }
    }
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [request.id, onAnswer]);

  return (
    <div className="modal-overlay">
      <div className="modal confirm-modal">
        <div className="confirm-alert">{request.risk || "This is irreversible."}</div>
        <p className="confirm-intro">
          I will not run <code>{request.name}</code> until you authorise it.
        </p>
        <pre className="confirm-preview">{request.preview}</pre>
        <p className="confirm-hint">Enter to authorise · Esc to stand down</p>
        <div className="confirm-actions">
          <button className="btn btn-ghost" onClick={() => onAnswer(request.id, false)}>
            Stand down
          </button>
          <button className="btn btn-primary" onClick={() => onAnswer(request.id, true)}>
            Authorise
          </button>
        </div>
      </div>
    </div>
  );
}
