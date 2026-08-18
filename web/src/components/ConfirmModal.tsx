import type { ConfirmationRequest } from "../types";

interface Props {
  request: ConfirmationRequest;
  onAnswer: (id: string, approved: boolean) => void;
}

export function ConfirmModal({ request, onAnswer }: Props) {
  return (
    <div className="modal-overlay">
      <div className="modal confirm-modal">
        <div className="confirm-alert">⚠ Authorisation required</div>
        <p className="confirm-intro">
          JARVIS wishes to perform a gated action via <code>{request.name}</code>.
        </p>
        <pre className="confirm-preview">{request.preview}</pre>
        <div className="confirm-actions">
          <button className="btn btn-ghost" onClick={() => onAnswer(request.id, false)}>
            Deny
          </button>
          <button className="btn btn-primary" onClick={() => onAnswer(request.id, true)}>
            Approve
          </button>
        </div>
      </div>
    </div>
  );
}
