export interface Conversation {
  id: number;
  title: string;
  created_at: number;
  updated_at: number;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  created_at?: number;
  attachments?: MediaAttachment[];
}

export interface MediaAttachment {
  id?: string;
  name: string;
  mime: string;
  kind?: "image" | "video" | "frame";
  url?: string;
  /** Local preview only (data URL) before/while sending */
  previewUrl?: string;
}

/** Payload sent over the WebSocket with a user message */
export interface OutgoingAttachment {
  name: string;
  mime: string;
  data: string; // base64
}

export type ActivityKind =
  | "thought"
  | "tool_call"
  | "tool_result"
  | "status"
  | "error"
  | "task";

export interface ActivityItem {
  id: string;
  kind: ActivityKind;
  name?: string;
  args?: Record<string, unknown>;
  output?: string;
  ok?: boolean;
  dangerous?: boolean;
  auto_approved?: boolean;
  text?: string;
  /** Chain-of-thought / <think> stream */
  reasoning?: string;
  /** Visible narration while tools run */
  content?: string;
  streaming?: boolean;
  state?: string;
  ts: number;
}

export interface ConfirmationRequest {
  id: string;
  name: string;
  args: Record<string, unknown>;
  preview: string;
}

export type AgentState =
  | "idle"
  | "thinking"
  | "running_tool"
  | "awaiting_confirmation";

export interface Device {
  id: number;
  name: string;
  fingerprint: string;
  profile: Record<string, any>;
  updated_at: number;
}

export interface JarvisConfig {
  host: string;
  port: number;
  hotkey: string;
  ptt_hotkey?: string;
  user_name: string;
  searxng_url: string;
  llm: {
    base_url: string;
    api_key: string;
    model: string;
    fallback_base_url: string;
    fallback_api_key: string;
    fallback_model: string;
    prefer: string;
    embedding_base_url: string;
    embedding_api_key: string;
    embedding_model: string;
    temperature: number;
  };
  email: EmailProfile;
  email_accounts: {
    default_id: string;
    profiles: EmailProfile[];
  };
  permissions: {
    auto_approve: boolean;
    sudo_password: string;
    sudo_configured?: boolean;
  };
  rem: {
    enabled: boolean;
    idle_minutes: number;
    min_interval_minutes: number;
  };
  telegram?: {
    enabled: boolean;
    bot_token: string;
    bot_token_configured?: boolean;
    allowed_chat_ids: string[];
    allowed_user_ids: string[];
    notify_tools: boolean;
  };
  tts?: {
    voice: string;
    rate: string;
    pitch: string;
  };
}

export interface EmailProfile {
  id: string;
  name: string;
  smtp_host: string;
  smtp_port: number;
  smtp_user: string;
  smtp_password: string;
  from_address: string;
  imap_host: string;
  imap_port: number;
  imap_user: string;
  imap_password: string;
  smtp_password_configured?: boolean;
  imap_password_configured?: boolean;
}

export interface RemStatus {
  enabled: boolean;
  phase: "awake" | "light" | "rem" | "deep" | "dreaming";
  running: boolean;
  idle_seconds: number;
  idle_minutes_config: number;
  min_interval_minutes: number;
  last_activity_at: number;
  last_sweep_at: number | null;
  last_result: string;
  seconds_until_eligible: number;
}

export interface RemLogEntry {
  id: string;
  phase: string;
  level: "info" | "entry" | "error" | string;
  text: string;
  ts: number;
}

export type TaskStatus = "running" | "completed" | "failed" | "cancelled";

export interface TaskLogEntry {
  /** Unix seconds */
  t: number;
  type: "thought" | "tool_call" | "tool_result" | "output" | "error" | "report" | string;
  text: string;
}

export interface BackgroundTask {
  id: string;
  kind: "agent" | "shell";
  title: string;
  goal: string;
  status: TaskStatus;
  conversation_id: number | null;
  result: string | null;
  log: TaskLogEntry[];
  created_at: number;
  finished_at: number | null;
}
