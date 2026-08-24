export interface Conversation {
  id: number;
  title: string;
  created_at: number;
  updated_at: number;
}

export interface SuggestionChip {
  label: string;
  prompt: string;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  created_at?: number;
  attachments?: MediaAttachment[];
  /** Mid-task spoken narration (not the final reply). */
  working?: boolean;
  /** Trailing optional next steps (not permission to do the original ask). */
  suggestions?: SuggestionChip[];
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
  | "task"
  | "peripheral";

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
  /** Short irreversible-risk line, e.g. "This will delete files." */
  risk?: string;
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

export type PeripheralKind =
  | "bluetooth"
  | "usb"
  | "audio"
  | "display"
  | "hid"
  | "camera"
  | "printer"
  | "storage"
  | "wifi"
  | "network"
  | "radio"
  | string;

export interface Peripheral {
  id: string;
  kind: PeripheralKind;
  name: string;
  address?: string;
  vendor?: string;
  product?: string;
  serial?: string;
  connected?: boolean;
  paired?: boolean;
  trusted?: boolean;
  available?: boolean;
  icon?: string;
  control_hints?: string[];
  identifiers?: Record<string, string>;
  extra?: Record<string, unknown>;
  facts?: string[];
  first_seen?: number;
  last_seen?: number;
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
  device?: {
    auto_learn?: boolean;
    refresh_hours?: number;
    scan_peripherals?: boolean;
    peripheral_refresh_minutes?: number;
  };
  telegram?: {
    enabled: boolean;
    bot_token: string;
    bot_token_configured?: boolean;
    allowed_chat_ids: string[];
    allowed_user_ids: string[];
    notify_tools: boolean;
  };
  mcp?: {
    enabled: boolean;
    servers: McpServerEntry[];
  };
  tts?: {
    voice: string;
    rate: string;
    pitch: string;
  };
}

export interface McpServerEntry {
  id: string;
  name: string;
  url: string;
  enabled: boolean;
  headers?: Record<string, string>;
  headers_configured?: boolean;
  /** "http" for a remote Streamable HTTP server, "stdio" for a local subprocess. */
  transport?: "http" | "stdio";
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  env_configured?: boolean;
  cwd?: string;
}

export interface McpRemoteTool {
  name: string;
  proxy_name: string;
  description: string;
  destructive?: boolean;
}

export interface McpServerStatus {
  id: string;
  name: string;
  url: string;
  enabled: boolean;
  connected: boolean;
  error?: string;
  tools: McpRemoteTool[];
}

export interface McpStatus {
  enabled: boolean;
  endpoint: string;
  servers: McpServerStatus[];
  imported_tool_count: number;
  exposed_tools: { name: string; description: string; dangerous: boolean }[];
  imported_tools: { name: string; description: string; dangerous: boolean }[];
}

export interface McpCatalogEntry {
  id: string;
  source: string;
  name: string;
  title: string;
  description: string;
  url: string;
  profile_url: string;
  repository: string;
  website_url?: string;
  icon_url?: string;
  version?: string;
  requires_auth: boolean;
  auth_hint: string;
  remote: boolean;
  tool_count: number;
  tool_hints: string[];
  tags: string[];
  installed: boolean;
}

export interface McpToolDetail {
  name: string;
  description: string;
}

export interface McpCatalogDetail extends McpCatalogEntry {
  tools_detail?: McpToolDetail[];
  remotes?: Record<string, unknown>[];
  packages?: Record<string, unknown>[];
  connections?: Record<string, unknown>[];
  verified?: boolean;
  use_count?: number;
  registry_meta?: Record<string, unknown>;
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

export interface MemoryEntry {
  id: number;
  kind: string;
  text: string;
  created_at: number;
}

export interface MemoryStats {
  total: number;
  by_kind: Record<string, number>;
  has_memory_md?: boolean;
  has_dreams_md?: boolean;
}

export interface MemoryListResponse {
  items: MemoryEntry[];
  total: number;
  limit: number;
  offset: number;
}

export interface MemoryFiles {
  memory_md: string;
  dreams_md: string;
}
