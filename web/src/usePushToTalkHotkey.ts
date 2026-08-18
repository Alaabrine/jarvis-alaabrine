/** Push-to-talk activation: Super+` (Backquote). Works globally in the desktop
 *  app via Tauri events, and as an in-window fallback in the browser. */

import { useEffect } from "react";

function isPttChord(e: KeyboardEvent): boolean {
  if (e.code !== "Backquote" && e.key !== "`") return false;
  // Super / Meta / Win key — Chromium reports Super as metaKey on Linux.
  return e.metaKey || e.getModifierState?.("OS") === true || e.getModifierState?.("Meta") === true;
}

export function usePushToTalkHotkey(handlers: {
  start: () => void;
  stop: () => void;
}) {
  const { start, stop } = handlers;

  // Desktop (Tauri): global Super+` → ptt-start / ptt-stop
  useEffect(() => {
    let disposed = false;
    const unlisteners: Array<() => void> = [];

    void (async () => {
      try {
        const { listen } = await import("@tauri-apps/api/event");
        if (disposed) return;
        unlisteners.push(
          await listen("ptt-start", () => {
            start();
          })
        );
        unlisteners.push(
          await listen("ptt-stop", () => {
            stop();
          })
        );
      } catch {
        /* not running inside Tauri */
      }
    })();

    return () => {
      disposed = true;
      for (const u of unlisteners) u();
    };
  }, [start, stop]);

  // In-window fallback (browser / focused desktop webview)
  useEffect(() => {
    const onDown = (e: KeyboardEvent) => {
      if (e.repeat) return;
      if (!isPttChord(e)) return;
      e.preventDefault();
      e.stopPropagation();
      start();
    };
    const onUp = (e: KeyboardEvent) => {
      // Stop when backtick or Super is released.
      if (
        e.code === "Backquote" ||
        e.key === "`" ||
        e.key === "Meta" ||
        e.key === "OS" ||
        e.code === "MetaLeft" ||
        e.code === "MetaRight" ||
        e.code === "OSLeft" ||
        e.code === "OSRight"
      ) {
        stop();
      }
    };
    window.addEventListener("keydown", onDown, true);
    window.addEventListener("keyup", onUp, true);
    return () => {
      window.removeEventListener("keydown", onDown, true);
      window.removeEventListener("keyup", onUp, true);
    };
  }, [start, stop]);
}
