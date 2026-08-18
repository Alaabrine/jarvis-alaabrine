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

export function useVoice(onTranscript: (text: string) => void) {
  const [listening, setListening] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  const [supported, setSupported] = useState(true);
  const [pttActive, setPttActive] = useState(false);

  const onTranscriptRef = useRef(onTranscript);
  onTranscriptRef.current = onTranscript;

  const listeningRef = useRef(false);
  const pttRef = useRef(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const objectUrlRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const speakGenRef = useRef(0);

  const mediaStreamRef = useRef<MediaStream | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<BlobPart[]>([]);
  const transcribeAbortRef = useRef<AbortController | null>(null);
  const recordStartedAtRef = useRef(0);

  const setListeningBoth = useCallback((v: boolean) => {
    listeningRef.current = v;
    setListening(v);
  }, []);

  useEffect(() => {
    setSupported(canRecordAudio());
  }, []);

  const hardStopAudio = useCallback(() => {
    speakGenRef.current += 1;
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

  const cleanupStream = useCallback(() => {
    const rec = recorderRef.current;
    recorderRef.current = null;
    if (rec && rec.state !== "inactive") {
      try {
        rec.stop();
      } catch {
        /* ignore */
      }
    }
    const stream = mediaStreamRef.current;
    mediaStreamRef.current = null;
    if (stream) {
      for (const track of stream.getTracks()) track.stop();
    }
  }, []);

  const startPushToTalk = useCallback(() => {
    if (pttRef.current) return;
    if (!canRecordAudio()) {
      console.warn("JARVIS PTT: MediaRecorder / getUserMedia unavailable");
      return;
    }

    hardStopAudio();
    transcribeAbortRef.current?.abort();
    transcribeAbortRef.current = null;
    cleanupStream();
    chunksRef.current = [];

    pttRef.current = true;
    setPttActive(true);
    setListeningBoth(true);

    void (async () => {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: {
            echoCancellation: true,
            noiseSuppression: true,
            channelCount: 1,
          },
        });
        if (!pttRef.current) {
          for (const track of stream.getTracks()) track.stop();
          return;
        }
        mediaStreamRef.current = stream;
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
      } catch (err) {
        console.error("JARVIS mic access failed:", err);
        pttRef.current = false;
        setPttActive(false);
        setListeningBoth(false);
        cleanupStream();
      }
    })();
  }, [cleanupStream, hardStopAudio, setListeningBoth]);

  const stopPushToTalk = useCallback(() => {
    if (!pttRef.current && !listeningRef.current) return;
    pttRef.current = false;
    setPttActive(false);

    const recorder = recorderRef.current;
    if (!recorder || recorder.state === "inactive") {
      cleanupStream();
      setListeningBoth(false);
      return;
    }

    const mime = recorder.mimeType || pickMimeType() || "audio/webm";
    const heldMs = Date.now() - (recordStartedAtRef.current || Date.now());

    recorder.onstop = () => {
      const chunks = chunksRef.current;
      chunksRef.current = [];
      cleanupStream();
      setListeningBoth(false);

      if (!chunks.length) {
        console.warn("JARVIS STT: no audio captured");
        return;
      }
      const blob = new Blob(chunks, { type: mime });
      // Ignore accidental taps / near-empty blobs.
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
      cleanupStream();
      setListeningBoth(false);
    }
  }, [cleanupStream, setListeningBoth]);

  // Click-to-talk aliases → same hold pipeline (start / stop)
  const startListening = startPushToTalk;
  const stopListening = stopPushToTalk;

  const stopSpeaking = hardStopAudio;

  const speak = useCallback(
    (text: string) => {
      const cleaned = (text || "").trim();
      if (!cleaned) return;

      stopSpeaking();
      const gen = speakGenRef.current;
      const ac = new AbortController();
      abortRef.current = ac;
      setSpeaking(true);

      void (async () => {
        try {
          const blob = await api.tts(cleaned, ac.signal);
          if (gen !== speakGenRef.current) return;

          const url = URL.createObjectURL(blob);
          objectUrlRef.current = url;
          const audio = new Audio(url);
          audioRef.current = audio;
          audio.onended = () => {
            if (gen !== speakGenRef.current) return;
            setSpeaking(false);
            if (objectUrlRef.current === url) {
              URL.revokeObjectURL(url);
              objectUrlRef.current = null;
            }
            audioRef.current = null;
          };
          audio.onerror = () => {
            if (gen !== speakGenRef.current) return;
            setSpeaking(false);
            if (objectUrlRef.current === url) {
              URL.revokeObjectURL(url);
              objectUrlRef.current = null;
            }
            audioRef.current = null;
          };
          await audio.play();
        } catch (err: any) {
          if (err?.name === "AbortError" || gen !== speakGenRef.current) return;
          console.error("JARVIS TTS failed:", err);
          setSpeaking(false);
        }
      })();
    },
    [stopSpeaking]
  );

  useEffect(
    () => () => {
      stopSpeaking();
      transcribeAbortRef.current?.abort();
      cleanupStream();
    },
    [cleanupStream, stopSpeaking]
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
