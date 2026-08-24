import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./overlay.css";

function Overlay() {
  return (
    <div className="overlay-root" data-tauri-drag-region>
      <div className="orb orb-listening">
        <div className="orb-core" />
        <div className="orb-ring" />
        <div className="orb-ring orb-ring-2" />
      </div>
      <p className="overlay-title">Listening</p>
      <p className="overlay-hint">Release Super+` to send</p>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Overlay />
  </StrictMode>
);
