import type {
  ActivityItem,
  BackgroundTask,
  ChatMessage,
  Conversation,
  Device,
  JarvisConfig,
  RemStatus,
} from "./types";

// In the Vite dev server we use its proxy (same origin). Elsewhere (e.g. the Tauri
// desktop shell) we talk to the local backend directly.
function backendBase(): string {
  const isViteDev =
    typeof location !== "undefined" &&
    (location.port === "5173" || location.hostname === "localhost");
  if (isViteDev && location.port === "5173") return "";
  return "http://127.0.0.1:8787";
}

export const API = backendBase();

export function mediaUrl(pathOrId: string): string {
  if (pathOrId.startsWith("http") || pathOrId.startsWith("data:") || pathOrId.startsWith("blob:")) {
    return pathOrId;
  }
  if (pathOrId.startsWith("/api/")) {
    return `${API}${pathOrId}`;
  }
  return `${API}/api/media/${pathOrId}`;
}

export function wsUrl(): string {
  if (API === "") {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${location.host}/ws/chat`;
  }
  return API.replace(/^http/, "ws") + "/ws/chat";
}

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as T;
}

export const api = {
  health: () => fetch(`${API}/api/health`).then((r) => json<{ status: string }>(r)),

  getConfig: () => fetch(`${API}/api/config`).then((r) => json<JarvisConfig>(r)),
  saveConfig: (data: Record<string, unknown>) =>
    fetch(`${API}/api/config`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ data }),
    }).then((r) => json<JarvisConfig>(r)),

  listConversations: () =>
    fetch(`${API}/api/conversations`).then((r) => json<Conversation[]>(r)),
  createConversation: () =>
    fetch(`${API}/api/conversations`, { method: "POST" }).then((r) =>
      json<{ id: number }>(r)
    ),
  renameConversation: (id: number, title: string) =>
    fetch(`${API}/api/conversations/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    }).then((r) => json(r)),
  deleteConversation: (id: number) =>
    fetch(`${API}/api/conversations/${id}`, { method: "DELETE" }).then((r) => json(r)),
  getMessages: (id: number) =>
    fetch(`${API}/api/conversations/${id}/messages`).then((r) => json<ChatMessage[]>(r)),

  getActivity: (id: number) =>
    fetch(`${API}/api/conversations/${id}/activity`).then((r) => json<ActivityItem[]>(r)),
  saveActivity: (id: number, items: ActivityItem[]) =>
    fetch(`${API}/api/conversations/${id}/activity`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items }),
    }).then((r) => json<{ ok: boolean }>(r)),
  clearActivity: (id: number) =>
    fetch(`${API}/api/conversations/${id}/activity`, { method: "DELETE" }).then((r) =>
      json<{ ok: boolean }>(r)
    ),

  listTasks: (status?: string) =>
    fetch(`${API}/api/tasks${status ? `?status=${encodeURIComponent(status)}` : ""}`).then(
      (r) => json<BackgroundTask[]>(r)
    ),
  getTask: (id: string) =>
    fetch(`${API}/api/tasks/${id}`).then((r) => json<BackgroundTask>(r)),
  cancelTask: (id: string) =>
    fetch(`${API}/api/tasks/${id}/cancel`, { method: "POST" }).then((r) =>
      json<{ ok: boolean }>(r)
    ),
  deleteTask: (id: string) =>
    fetch(`${API}/api/tasks/${id}`, { method: "DELETE" }).then((r) => json<{ ok: boolean }>(r)),

  remStatus: () => fetch(`${API}/api/rem/status`).then((r) => json<RemStatus>(r)),
  remRun: () =>
    fetch(`${API}/api/rem/run`, { method: "POST" }).then((r) =>
      json<{ ok: boolean; message: string }>(r)
    ),
  remWake: () =>
    fetch(`${API}/api/rem/wake`, { method: "POST" }).then((r) => json<RemStatus>(r)),

  listDevices: () => fetch(`${API}/api/devices`).then((r) => json<Device[]>(r)),

  refreshDevice: () =>
    fetch(`${API}/api/devices/refresh`, { method: "POST" }).then((r) =>
      json<{ ok: boolean; hostname: string; summary: string }>(r)
    ),

  /** Synthesize JARVIS butler speech; returns an audio/mpeg Blob. */
  tts: (text: string, signal?: AbortSignal) =>
    fetch(`${API}/api/tts`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
      signal,
    }).then(async (r) => {
      if (!r.ok) {
        const detail = await r.text().catch(() => r.statusText);
        throw new Error(detail || `${r.status} ${r.statusText}`);
      }
      return r.blob();
    }),

  /** Transcribe push-to-talk audio; returns plain text. */
  stt: async (blob: Blob, signal?: AbortSignal) => {
    const form = new FormData();
    const ext = blob.type.includes("mp4") ? "mp4" : blob.type.includes("ogg") ? "ogg" : "webm";
    form.append("file", blob, `ptt.${ext}`);
    const r = await fetch(`${API}/api/stt`, { method: "POST", body: form, signal });
    if (!r.ok) {
      const detail = await r.text().catch(() => r.statusText);
      throw new Error(detail || `${r.status} ${r.statusText}`);
    }
    const data = (await r.json()) as { text?: string };
    return (data.text || "").trim();
  },
};
