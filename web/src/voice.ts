// Voice I/O for JARVIS.
//
// Push-to-talk records mic audio with MediaRecorder (works in Tauri/WebKit) and
// transcribes via POST /api/stt — independent of the browser Web Speech API.
// Text-to-speech uses POST /api/tts.

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";

function canRecordAudio(): boolean {
  return typeof navigator !== "undefined"
    && !!navigator.mediaDevices?.getUserMedia
    && typeof MediaRecorder !== "undefined";
}

function pickMimeType(): string {
  // Prefer container formats WebKit/Chromium both decode reliably via ffmpeg.
  const candidates = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/ogg;codecs=opus",
    "audio/mp4",
  ];
  for (const t of candidates) {
    try {
      if (typeof MediaRecorder !== "undefined" && MediaRecorder.isTypeSupported?.(t)) {
        return t;
      }
    } catch {
      /* ignore */
    }
  }
  return "";
}

const MIC_CONSTRAINTS: MediaTrackConstraints = {
  echoCancellation: true,
  noiseSuppression: true,
  channelCount: 1,
};

export function useVoice(onTranscript: (text: string) => void) {
  const [listening, setListening] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  const [supported, setSupported] = useState(true);
  const [pttActive, setPttActive] = useState(false);

  const onTranscriptRef = useRef(onTranscript);
  onTranscriptRef.current = onTranscript;

  const listeningRef = useRef(false);
  const wantRecordingRef = useRef(false);
  const pttRef = useRef(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const objectUrlRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const speakGenRef = useRef(0);

  const micStreamRef = useRef<MediaStream | null>(null);
  const micAcquireRef = useRef<Promise<MediaStream | null> | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<BlobPart[]>([]);
  const transcribeAbortRef = useRef<AbortController | null>(null);
  const recordStartedAtRef = useRef(0);
  const setupGenRef = useRef(0);
  const queueRef = useRef<string[]>([]);
  const pumpingRef = useRef(false);

  const setListeningBoth = useCallback((v: boolean) => {
    listeningRef.current = v;
    setListening(v);
  }, []);

  useEffect(() => {
    setSupported(canRecordAudio());
  }, []);

  const hardStopAudio = useCallback(() => {
    speakGenRef.current += 1;
    queueRef.current = [];
    pumpingRef.current = false;
    abortRef.current?.abort();
    abortRef.current = null;
    const audio = audioRef.current;
    if (audio) {
      audio.onended = null;
      audio.onerror = null;
      try {
        audio.pause();
      } catch {
        /* ignore */
      }
      audio.removeAttribute("src");
      audio.load();
      audioRef.current = null;
    }
    if (objectUrlRef.current) {
      URL.revokeObjectURL(objectUrlRef.current);
      objectUrlRef.current = null;
    }
    setSpeaking(false);
  }, []);

  const stopRecorderOnly = useCallback(() => {
    const rec = recorderRef.current;
    recorderRef.current = null;
    if (rec && rec.state !== "inactive") {
      try {
        rec.stop();
      } catch {
        /* ignore */
      }
    }
  }, []);

  const releaseMicStream = useCallback(() => {
    stopRecorderOnly();
    micAcquireRef.current = null;
    const stream = micStreamRef.current;
    micStreamRef.current = null;
    if (stream) {
      for (const track of stream.getTracks()) track.stop();
    }
  }, [stopRecorderOnly]);

  const acquireMicStream = useCallback(async (): Promise<MediaStream | null> => {
    const existing = micStreamRef.current;
    if (existing?.active && existing.getAudioTracks().some((t) => t.readyState === "live")) {
      return existing;
    }
    if (existing) {
      for (const track of existing.getTracks()) track.stop();
      micStreamRef.current = null;
    }
    if (micAcquireRef.current) return micAcquireRef.current;

    const pending = navigator.mediaDevices
      .getUserMedia({ audio: MIC_CONSTRAINTS })
      .then((stream) => {
        micStreamRef.current = stream;
        micAcquireRef.current = null;
        return stream;
      })
      .catch((err) => {
        micAcquireRef.current = null;
        throw err;
      });
    micAcquireRef.current = pending;
    return pending;
  }, []);

  const finalizeRecording = useCallback(
    (mime: string, heldMs: number) => {
      const chunks = chunksRef.current;
      chunksRef.current = [];
      setListeningBoth(false);

      if (!chunks.length) {
        console.warn("JARVIS STT: no audio captured");
        return;
      }
      const blob = new Blob(chunks, { type: mime });
      if (blob.size < 512 || heldMs < 250) {
        console.warn("JARVIS STT: recording too short", { size: blob.size, heldMs });
        return;
      }

      const ac = new AbortController();
      transcribeAbortRef.current = ac;
      void (async () => {
        try {
          const text = await api.stt(blob, ac.signal);
          if (text.trim()) {
            onTranscriptRef.current(text.trim());
          } else {
            console.warn("JARVIS STT: no speech detected — hold longer and speak clearly");
          }
        } catch (err: any) {
          if (err?.name === "AbortError") return;
          console.error("JARVIS STT failed:", err);
        } finally {
          if (transcribeAbortRef.current === ac) transcribeAbortRef.current = null;
        }
      })();
    },
    [setListeningBoth]
  );

  const stopPushToTalk = useCallback(() => {
    if (!pttRef.current && !listeningRef.current) return;

    wantRecordingRef.current = false;
    pttRef.current = false;
    setPttActive(false);

    const recorder = recorderRef.current;
    if (!recorder || recorder.state === "inactive") {
      // Mic may still be opening — setup will stop once the recorder is ready.
      if (!micAcquireRef.current) setListeningBoth(false);
      return;
    }

    const mime = recorder.mimeType || pickMimeType() || "audio/webm";
    const heldMs = Date.now() - (recordStartedAtRef.current || Date.now());

    recorder.onstop = () => {
      recorderRef.current = null;
      finalizeRecording(mime, heldMs);
    };

    try {
      if (recorder.state === "recording") {
        try {
          recorder.requestData();
        } catch {
          /* some browsers throw if unsupported */
        }
      }
      recorder.stop();
    } catch {
      recorderRef.current = null;
      setListeningBoth(false);
    }
  }, [finalizeRecording, setListeningBoth]);

  const startPushToTalk = useCallback(() => {
    if (pttRef.current) return;
    if (!canRecordAudio()) {
      console.warn("JARVIS PTT: MediaRecorder / getUserMedia unavailable");
      return;
    }

    hardStopAudio();
    transcribeAbortRef.current?.abort();
    transcribeAbortRef.current = null;

    // Stop any in-flight recorder but keep the mic stream warm for the next hold.
    stopRecorderOnly();
    chunksRef.current = [];

    wantRecordingRef.current = true;
    pttRef.current = true;
    setPttActive(true);
    setListeningBoth(true);

    const gen = ++setupGenRef.current;

    void (async () => {
      try {
        const stream = await acquireMicStream();
        if (gen !== setupGenRef.current) return;

        if (!stream || !wantRecordingRef.current) {
          pttRef.current = false;
          setPttActive(false);
          setListeningBoth(false);
          return;
        }

        const mime = pickMimeType();
        const recorder = mime
          ? new MediaRecorder(stream, { mimeType: mime })
          : new MediaRecorder(stream);
        recorderRef.current = recorder;
        chunksRef.current = [];
        recorder.ondataavailable = (ev) => {
          if (ev.data && ev.data.size > 0) chunksRef.current.push(ev.data);
        };
        recorder.onerror = (ev: any) => {
          console.error("JARVIS MediaRecorder error:", ev?.error || ev);
        };
        // No timeslice: WebKit chunked WebM often fails to decode when concatenated.
        recorder.start();
        recordStartedAtRef.current = Date.now();

        if (!wantRecordingRef.current) {
          stopPushToTalk();
        }
      } catch (err) {
        if (gen !== setupGenRef.current) return;
        console.error("JARVIS mic access failed:", err);
        wantRecordingRef.current = false;
        pttRef.current = false;
        setPttActive(false);
        setListeningBoth(false);
      }
    })();
  }, [
    acquireMicStream,
    hardStopAudio,
    setListeningBoth,
    stopPushToTalk,
    stopRecorderOnly,
  ]);

  // Click-to-talk aliases → same hold pipeline (start / stop)
  const startListening = startPushToTalk;
  const stopListening = stopPushToTalk;

  const stopSpeaking = hardStopAudio;

  const speak = useCallback((text: string) => {
    const cleaned = (text || "").trim();
    if (!cleaned) return;
    queueRef.current.push(cleaned);
    if (pumpingRef.current) {
      setSpeaking(true);
      return;
    }
    pumpingRef.current = true;
    setSpeaking(true);

    const pump = async () => {
      const genAtStart = speakGenRef.current;
      try {
        while (queueRef.current.length) {
          const next = queueRef.current.shift();
          if (!next) break;
          const gen = speakGenRef.current;
          const ac = new AbortController();
          abortRef.current = ac;
          try {
            const blob = await api.tts(next, ac.signal);
            if (gen !== speakGenRef.current) return;
            const url = URL.createObjectURL(blob);
            objectUrlRef.current = url;
            const audio = new Audio(url);
            audioRef.current = audio;
            await new Promise<void>((resolve) => {
              audio.onended = () => resolve();
              audio.onerror = () => resolve();
              void audio.play().catch(() => resolve());
            });
            if (objectUrlRef.current === url) {
              URL.revokeObjectURL(url);
              objectUrlRef.current = null;
            }
            audioRef.current = null;
            if (gen !== speakGenRef.current) return;
          } catch (err: any) {
            if (err?.name === "AbortError" || gen !== speakGenRef.current) return;
            console.error("JARVIS TTS failed:", err);
          }
        }
      } finally {
        if (speakGenRef.current === genAtStart) {
          pumpingRef.current = false;
          setSpeaking(false);
        }
      }
    };

    void pump();
  }, []);

  // Pre-open the mic so the first push-to-talk hold is responsive (desktop WebKit).
  useEffect(() => {
    if (!canRecordAudio()) return;
    void acquireMicStream().catch(() => {
      /* permission prompt may wait for user interaction */
    });
  }, [acquireMicStream]);

  useEffect(
    () => () => {
      stopSpeaking();
      transcribeAbortRef.current?.abort();
      releaseMicStream();
    },
    [releaseMicStream, stopSpeaking]
  );

  return {
    listening,
    speaking,
    supported,
    pttActive,
    startListening,
    stopListening,
    startPushToTalk,
    stopPushToTalk,
    speak,
    stopSpeaking,
  };
}
