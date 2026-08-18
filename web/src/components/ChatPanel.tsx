import { useEffect, useRef, useState } from "react";
import { mediaUrl } from "../api";
import type { AgentState, ChatMessage, MediaAttachment, OutgoingAttachment } from "../types";
import { MessageContent } from "./MessageContent";

const MAX_ATTACHMENTS = 6;
const MAX_BYTES = 40 * 1024 * 1024;
const ACCEPT = "image/jpeg,image/png,image/webp,image/gif,video/mp4,video/webm,video/quicktime";

interface VoiceApi {
  listening: boolean;
  speaking: boolean;
  supported: boolean;
  pttActive: boolean;
  startListening: () => void;
  stopListening: () => void;
  startPushToTalk: () => void;
  stopPushToTalk: () => void;
  stopSpeaking: () => void;
}

interface PendingFile {
  id: string;
  file: File;
  previewUrl: string;
  kind: "image" | "video";
}

interface Props {
  messages: ChatMessage[];
  agentState: AgentState;
  onSend: (
    text: string,
    attachments: OutgoingAttachment[],
    localMeta?: MediaAttachment[]
  ) => void;
  onInterrupt: () => void;
  voice: VoiceApi;
}

async function fileToOutgoing(file: File): Promise<OutgoingAttachment> {
  const buf = await file.arrayBuffer();
  const bytes = new Uint8Array(buf);
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return {
    name: file.name,
    mime: file.type || "application/octet-stream",
    data: btoa(binary),
  };
}

export function ChatPanel({ messages, agentState, onSend, onInterrupt, voice }: Props) {
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState<PendingFile[]>([]);
  const [attachError, setAttachError] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [messages, agentState]);

  useEffect(() => {
    return () => {
      pending.forEach((p) => URL.revokeObjectURL(p.previewUrl));
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function submit() {
    if (!draft.trim() && pending.length === 0) return;
    const files = [...pending];
    setPending([]);
    setAttachError(null);
    const text = draft;
    setDraft("");
    try {
      const attachments = await Promise.all(files.map((p) => fileToOutgoing(p.file)));
      const localMeta: MediaAttachment[] = files.map((p) => ({
        name: p.file.name,
        mime: p.file.type || "application/octet-stream",
        kind: p.kind,
        previewUrl: p.previewUrl,
      }));
      // Keep object URLs for bubble thumbnails (revoked on page unload).
      onSend(text, attachments, localMeta);
    } catch (err) {
      setAttachError(err instanceof Error ? err.message : "Failed to read attachments");
      setPending(files);
    }
  }

  function onPickFiles(list: FileList | null) {
    if (!list?.length) return;
    setAttachError(null);
    const next: PendingFile[] = [];
    for (const file of Array.from(list)) {
      if (pending.length + next.length >= MAX_ATTACHMENTS) {
        setAttachError(`Maximum ${MAX_ATTACHMENTS} attachments per message.`);
        break;
      }
      if (file.size > MAX_BYTES) {
        setAttachError(`"${file.name}" is too large (max 40 MB).`);
        continue;
      }
      const isImage = file.type.startsWith("image/");
      const isVideo = file.type.startsWith("video/");
      if (!isImage && !isVideo) {
        setAttachError(`Unsupported type: ${file.type || file.name}`);
        continue;
      }
      next.push({
        id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        file,
        previewUrl: URL.createObjectURL(file),
        kind: isVideo ? "video" : "image",
      });
    }
    if (next.length) setPending((prev) => [...prev, ...next]);
    if (fileRef.current) fileRef.current.value = "";
  }

  function removePending(id: string) {
    setPending((prev) => {
      const target = prev.find((p) => p.id === id);
      if (target) URL.revokeObjectURL(target.previewUrl);
      return prev.filter((p) => p.id !== id);
    });
  }

  const busy =
    agentState === "thinking" ||
    agentState === "running_tool" ||
    agentState === "awaiting_confirmation";
  const canSend = Boolean(draft.trim() || pending.length);

  return (
    <section className="chat-panel">
      <div className="messages" ref={scrollRef}>
        {messages.length === 0 && (
          <div className="welcome">
            <p className="welcome-title">Good day. JARVIS at your service.</p>
            <p className="welcome-sub">
              Ask me anything, or attach images and videos for analysis. I can also manage
              your files, run commands, launch apps, and search the web — with your approval
              where it matters.
            </p>
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`bubble bubble-${m.role}`}>
            <div className="bubble-role">{m.role === "user" ? "You" : "JARVIS"}</div>
            {m.attachments && m.attachments.length > 0 && (
              <div className="bubble-media">
                {m.attachments.map((a, ai) => (
                  <AttachmentThumb key={a.id || ai} attachment={a} />
                ))}
              </div>
            )}
            {m.content && <MessageContent role={m.role} content={m.content} />}
          </div>
        ))}
        {busy && agentState !== "awaiting_confirmation" && (
          <div className="bubble bubble-assistant">
            <div className="bubble-role">JARVIS</div>
            <div className="typing">
              <span /> <span /> <span />
            </div>
          </div>
        )}
      </div>

      {(pending.length > 0 || attachError) && (
        <div className="attach-tray">
          {attachError && <div className="attach-error">{attachError}</div>}
          <div className="attach-previews">
            {pending.map((p) => (
              <div key={p.id} className="attach-chip">
                {p.kind === "video" ? (
                  <video src={p.previewUrl} muted />
                ) : (
                  <img src={p.previewUrl} alt={p.file.name} />
                )}
                <span className="attach-name">{p.file.name}</span>
                <button type="button" className="attach-remove" onClick={() => removePending(p.id)} title="Remove">
                  ×
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className={`composer ${voice.listening || voice.pttActive ? "listening" : ""}`}>
        <input
          ref={fileRef}
          type="file"
          accept={ACCEPT}
          multiple
          hidden
          onChange={(e) => onPickFiles(e.target.files)}
        />
        <button
          type="button"
          className="attach-btn"
          title="Attach image or video"
          disabled={busy}
          onClick={() => fileRef.current?.click()}
        >
          ＋
        </button>
        <textarea
          value={draft}
          placeholder={
            voice.pttActive
              ? "Push-to-talk — release to send…"
              : voice.listening
                ? "Listening…"
                : pending.length
                  ? "Add a caption or question about the media…"
                  : "Hold mic or Super+` to speak…"
          }
          onChange={(e) => setDraft(e.target.value)}
          onPaste={(e) => {
            const files = e.clipboardData?.files;
            if (files?.length) {
              e.preventDefault();
              onPickFiles(files);
            }
          }}
          onKeyDown={(e) => {
            if (e.key === "Escape" && busy) {
              e.preventDefault();
              onInterrupt();
              return;
            }
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void submit();
            }
          }}
          rows={1}
        />
        <button
          type="button"
          className={`mic-btn ${voice.pttActive || voice.listening ? "mic-on" : ""}`}
          title={
            voice.supported
              ? "Hold to talk — release to send"
              : "Microphone recording unavailable"
          }
          disabled={!voice.supported}
          aria-label="Hold to talk"
          onPointerDown={(e) => {
            if (!voice.supported || e.button !== 0) return;
            e.preventDefault();
            (e.currentTarget as HTMLButtonElement).setPointerCapture(e.pointerId);
            voice.startPushToTalk();
          }}
          onPointerUp={(e) => {
            if (!voice.supported) return;
            try {
              (e.currentTarget as HTMLButtonElement).releasePointerCapture(e.pointerId);
            } catch {
              /* ignore */
            }
            voice.stopPushToTalk();
          }}
          onPointerCancel={() => voice.stopPushToTalk()}
          onLostPointerCapture={() => voice.stopPushToTalk()}
          onContextMenu={(e) => e.preventDefault()}
        >
          <MicIcon active={voice.pttActive || voice.listening} />
        </button>
        {busy ? (
          <button className="stop-btn" onClick={onInterrupt} title="Interrupt (Esc)">
            Stop
          </button>
        ) : (
          <button className="send-btn" onClick={() => void submit()} disabled={!canSend}>
            Send
          </button>
        )}
      </div>
    </section>
  );
}

function AttachmentThumb({ attachment }: { attachment: MediaAttachment }) {
  const src = attachment.previewUrl || (attachment.url ? mediaUrl(attachment.url) : attachment.id ? mediaUrl(attachment.id) : "");
  const kind = attachment.kind || (attachment.mime?.startsWith("video/") ? "video" : "image");
  if (!src) {
    return <div className="bubble-media-fallback">{attachment.name}</div>;
  }
  if (kind === "video") {
    return (
      <a className="bubble-media-item" href={src} target="_blank" rel="noreferrer" title={attachment.name}>
        <video src={src} muted />
        <span className="media-badge">video</span>
      </a>
    );
  }
  return (
    <a className="bubble-media-item" href={src} target="_blank" rel="noreferrer" title={attachment.name}>
      <img src={src} alt={attachment.name} />
    </a>
  );
}

function MicIcon({ active }: { active: boolean }) {
  return (
    <svg
      className="mic-icon"
      viewBox="0 0 24 24"
      width="22"
      height="22"
      aria-hidden="true"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <rect x="9" y="2" width="6" height="11" rx="3" fill={active ? "currentColor" : "none"} />
      <path d="M5 11a7 7 0 0 0 14 0" />
      <line x1="12" y1="18" x2="12" y2="22" />
      <line x1="8" y1="22" x2="16" y2="22" />
    </svg>
  );
}
