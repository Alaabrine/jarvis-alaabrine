/** Hold-to-talk mic button — mouse/touch only.
 *
 * WebKitGTK (Tauri on Linux) mishandles Pointer Events: setPointerCapture often
 * fails and fires pointercancel immediately, so a "hold" becomes a single tap.
 * Document-level mouseup/touchend is reliable across browsers and Tauri. */

import { useCallback, useEffect, useRef } from "react";

interface MicVoice {
  supported: boolean;
  startPushToTalk: () => void;
  stopPushToTalk: () => void;
}

export function useMicHold(voice: MicVoice) {
  const holdingRef = useRef(false);
  const voiceRef = useRef(voice);
  voiceRef.current = voice;

  const beginMicHold = useCallback(() => {
    if (!voiceRef.current.supported || holdingRef.current) return;
    holdingRef.current = true;
    voiceRef.current.startPushToTalk();
  }, []);

  const endMicHold = useCallback(() => {
    if (!holdingRef.current) return;
    holdingRef.current = false;
    voiceRef.current.stopPushToTalk();
  }, []);

  useEffect(() => {
    const onMouseUp = (e: MouseEvent) => {
      if (e.button !== 0) return;
      endMicHold();
    };
    const onTouchEnd = () => endMicHold();
    document.addEventListener("mouseup", onMouseUp, true);
    document.addEventListener("touchend", onTouchEnd, true);
    document.addEventListener("touchcancel", onTouchEnd, true);
    return () => {
      document.removeEventListener("mouseup", onMouseUp, true);
      document.removeEventListener("touchend", onTouchEnd, true);
      document.removeEventListener("touchcancel", onTouchEnd, true);
    };
  }, [endMicHold]);

  return { beginMicHold, endMicHold };
}
