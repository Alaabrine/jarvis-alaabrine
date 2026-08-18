"""Runtime configuration for JARVIS.

Configuration is layered: environment variables provide defaults, and a JSON file in the
data directory (editable from the Settings UI) overrides them at runtime.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Lock

DATA_DIR = Path(os.environ.get("JARVIS_DATA_DIR", Path.home() / ".jarvis"))
CONFIG_PATH = DATA_DIR / "config.json"


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class LLMConfig:
    """Primary (local, e.g. Odysseus) endpoint plus an optional cloud fallback."""

    base_url: str = field(default_factory=lambda: _env("JARVIS_LLM_BASE_URL", default="http://localhost:11434/v1"))
    api_key: str = field(default_factory=lambda: _env("JARVIS_LLM_API_KEY", default="local"))
    model: str = field(default_factory=lambda: _env("JARVIS_LLM_MODEL", default="qwen2.5:latest"))

    fallback_base_url: str = field(default_factory=lambda: _env("JARVIS_FALLBACK_BASE_URL"))
    fallback_api_key: str = field(default_factory=lambda: _env("JARVIS_FALLBACK_API_KEY"))
    fallback_model: str = field(default_factory=lambda: _env("JARVIS_FALLBACK_MODEL"))

    # Which endpoint to prefer: "local" (Odysseus) or "cloud".
    prefer: str = field(default_factory=lambda: _env("JARVIS_LLM_PREFER", default="local"))

    embedding_base_url: str = field(default_factory=lambda: _env("JARVIS_EMBEDDING_BASE_URL"))
    embedding_api_key: str = field(default_factory=lambda: _env("JARVIS_EMBEDDING_API_KEY"))
    embedding_model: str = field(default_factory=lambda: _env("JARVIS_EMBEDDING_MODEL"))

    temperature: float = 0.6


@dataclass
class EmailProfile:
    """One SMTP/IMAP mailbox identity JARVIS can send/read as."""

    id: str = "default"
    name: str = "Default"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    from_address: str = ""
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""


@dataclass
class EmailAccounts:
    """Named email profiles with a selectable default."""

    default_id: str = "default"
    profiles: list[EmailProfile] = field(default_factory=list)


def _default_email_accounts() -> EmailAccounts:
    """Seed a default profile from environment variables (legacy single-account)."""
    profile = EmailProfile(
        id="default",
        name="Default",
        smtp_host=_env("JARVIS_SMTP_HOST"),
        smtp_port=int(_env("JARVIS_SMTP_PORT", default="587")),
        smtp_user=_env("JARVIS_SMTP_USER"),
        smtp_password=_env("JARVIS_SMTP_PASSWORD"),
        from_address=_env("JARVIS_EMAIL_FROM"),
        imap_host=_env("JARVIS_IMAP_HOST"),
        imap_port=int(_env("JARVIS_IMAP_PORT", default="993")),
        imap_user=_env("JARVIS_IMAP_USER"),
        imap_password=_env("JARVIS_IMAP_PASSWORD"),
    )
    return EmailAccounts(default_id="default", profiles=[profile])


# Backward-compatible alias used by older call sites / docs.
EmailConfig = EmailProfile


@dataclass
class PermissionsConfig:
    """Safety / privilege settings editable from the UI."""

    # When True, gated (destructive) tool calls run without an Approve/Deny prompt.
    auto_approve: bool = field(default_factory=lambda: _env_bool("JARVIS_AUTO_APPROVE", False))
    # Optional sudo password so JARVIS can run `sudo` via `sudo -S` (stdin). Stored locally.
    sudo_password: str = field(default_factory=lambda: _env("JARVIS_SUDO_PASSWORD"))


@dataclass
class RemConfig:
    """Inactivity-triggered REM memory consolidation (OpenClaw-style dreaming)."""

    enabled: bool = field(default_factory=lambda: _env_bool("JARVIS_REM_ENABLED", True))
    idle_minutes: int = field(default_factory=lambda: int(_env("JARVIS_REM_IDLE_MINUTES", default="15")))
    min_interval_minutes: int = field(
        default_factory=lambda: int(_env("JARVIS_REM_MIN_INTERVAL_MINUTES", default="60"))
    )


@dataclass
class TtsConfig:
    """Server-side neural TTS (edge-tts). Independent of browser/OS speech APIs."""

    # en-GB-ThomasNeural: formal British male — butler cadence with rate/pitch below.
    voice: str = field(default_factory=lambda: _env("JARVIS_TTS_VOICE", default="en-GB-ThomasNeural"))
    rate: str = field(default_factory=lambda: _env("JARVIS_TTS_RATE", default="-12%"))
    pitch: str = field(default_factory=lambda: _env("JARVIS_TTS_PITCH", default="-6Hz"))


@dataclass
class SttConfig:
    """Local speech-to-text via faster-whisper (free / offline after first download)."""

    # tiny.en | base.en | small.en | … — English models are faster for JARVIS PTT.
    model: str = field(default_factory=lambda: _env("JARVIS_STT_MODEL", default="base.en"))
    device: str = field(default_factory=lambda: _env("JARVIS_STT_DEVICE", default="cpu"))
    compute_type: str = field(default_factory=lambda: _env("JARVIS_STT_COMPUTE_TYPE", default="int8"))


def _parse_id_list(raw: str) -> list[str]:
    """Parse a comma/space-separated list of Telegram chat or user ids."""
    if not raw or not str(raw).strip():
        return []
    parts = [p.strip() for p in str(raw).replace(";", ",").replace(" ", ",").split(",")]
    return [p for p in parts if p]


@dataclass
class TelegramConfig:
    """Remote chat via Telegram Bot API (outbound long-polling; no port exposure)."""

    enabled: bool = field(default_factory=lambda: _env_bool("JARVIS_TELEGRAM_ENABLED", False))
    bot_token: str = field(default_factory=lambda: _env("JARVIS_TELEGRAM_BOT_TOKEN"))
    # Comma-separated chat ids and/or user ids allowed to talk to JARVIS.
    # Required when enabled — empty allowlist rejects everyone (use /whoami to learn yours).
    allowed_chat_ids: list[str] = field(
        default_factory=lambda: _parse_id_list(_env("JARVIS_TELEGRAM_ALLOWED_CHAT_IDS"))
    )
    allowed_user_ids: list[str] = field(
        default_factory=lambda: _parse_id_list(_env("JARVIS_TELEGRAM_ALLOWED_USER_IDS"))
    )
    # Stream brief tool / status updates into the Telegram chat.
    notify_tools: bool = field(default_factory=lambda: _env_bool("JARVIS_TELEGRAM_NOTIFY_TOOLS", True))


@dataclass
class Config:
    host: str = field(default_factory=lambda: _env("JARVIS_HOST", default="127.0.0.1"))
    port: int = field(default_factory=lambda: int(_env("JARVIS_PORT", default="8787")))
    hotkey: str = field(default_factory=lambda: _env("JARVIS_HOTKEY", default="Ctrl+Alt+J"))
    # Desktop push-to-talk (hold to dictate). Tauri parses e.g. Super+Backquote.
    ptt_hotkey: str = field(
        default_factory=lambda: _env("JARVIS_PTT_HOTKEY", default="Super+Backquote")
    )
    # SearXNG or other search endpoint used by the web_search tool.
    search_url: str = field(default_factory=lambda: _env("JARVIS_SEARCH_URL", default="https://duckduckgo.com/html/"))
    searxng_url: str = field(default_factory=lambda: _env("JARVIS_SEARXNG_URL"))
    user_name: str = field(default_factory=lambda: _env("JARVIS_USER_NAME", default="Sir"))
    llm: LLMConfig = field(default_factory=LLMConfig)
    email_accounts: EmailAccounts = field(default_factory=_default_email_accounts)
    permissions: PermissionsConfig = field(default_factory=PermissionsConfig)
    rem: RemConfig = field(default_factory=RemConfig)
    tts: TtsConfig = field(default_factory=TtsConfig)
    stt: SttConfig = field(default_factory=SttConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)

    def get_email_profile(self, profile_ref: str | None = None) -> EmailProfile:
        """Resolve a profile by id or name; falls back to the default profile."""
        accounts = self.email_accounts
        profiles = accounts.profiles or _default_email_accounts().profiles
        if not profiles:
            return EmailProfile()

        ref = (profile_ref or "").strip()
        if not ref:
            ref = accounts.default_id or profiles[0].id

        for p in profiles:
            if p.id == ref:
                return p
        lowered = ref.lower()
        for p in profiles:
            if p.name.lower() == lowered:
                return p
        # Unknown ref — use default id, then first profile.
        for p in profiles:
            if p.id == accounts.default_id:
                return p
        return profiles[0]

    @property
    def email(self) -> EmailProfile:
        """Default email profile (compat for older code paths)."""
        return self.get_email_profile()


class ConfigStore:
    """Thread-safe, persisted configuration."""

    def __init__(self) -> None:
        self._lock = Lock()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._config = self._load()

    def _load(self) -> Config:
        cfg = Config()
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text())
                self._apply(cfg, data)
            except (json.JSONDecodeError, OSError):
                pass
        return cfg

    @staticmethod
    def _apply(cfg: Config, data: dict) -> None:
        for key, value in data.items():
            if key == "llm" and isinstance(value, dict):
                for k, v in value.items():
                    if hasattr(cfg.llm, k):
                        setattr(cfg.llm, k, v)
            elif key == "email_accounts" and isinstance(value, dict):
                ConfigStore._apply_email_accounts(cfg, value)
            elif key == "email" and isinstance(value, dict):
                # Legacy single-account blob → merge into default profile.
                ConfigStore._apply_legacy_email(cfg, value)
            elif key == "permissions" and isinstance(value, dict):
                for k, v in value.items():
                    if hasattr(cfg.permissions, k):
                        if k == "auto_approve":
                            setattr(cfg.permissions, k, bool(v))
                        else:
                            setattr(cfg.permissions, k, v)
            elif key == "rem" and isinstance(value, dict):
                for k, v in value.items():
                    if hasattr(cfg.rem, k):
                        if k == "enabled":
                            setattr(cfg.rem, k, bool(v))
                        elif k in {"idle_minutes", "min_interval_minutes"}:
                            setattr(cfg.rem, k, int(v))
                        else:
                            setattr(cfg.rem, k, v)
            elif key == "tts" and isinstance(value, dict):
                for k, v in value.items():
                    if hasattr(cfg.tts, k) and v is not None:
                        setattr(cfg.tts, k, str(v))
            elif key == "stt" and isinstance(value, dict):
                for k, v in value.items():
                    if hasattr(cfg.stt, k) and v is not None:
                        setattr(cfg.stt, k, str(v))
            elif key == "telegram" and isinstance(value, dict):
                ConfigStore._apply_telegram(cfg, value)
            elif hasattr(cfg, key) and key not in {"email"}:
                setattr(cfg, key, value)
        ConfigStore._ensure_email_accounts(cfg)

    @staticmethod
    def _apply_telegram(cfg: Config, value: dict) -> None:
        tg = cfg.telegram
        if "enabled" in value:
            tg.enabled = bool(value["enabled"])
        if "bot_token" in value and value["bot_token"] is not None:
            tg.bot_token = str(value["bot_token"])
        if "notify_tools" in value:
            tg.notify_tools = bool(value["notify_tools"])
        if "allowed_chat_ids" in value:
            raw = value["allowed_chat_ids"]
            if isinstance(raw, list):
                tg.allowed_chat_ids = [str(x).strip() for x in raw if str(x).strip()]
            else:
                tg.allowed_chat_ids = _parse_id_list(str(raw or ""))
        if "allowed_user_ids" in value:
            raw = value["allowed_user_ids"]
            if isinstance(raw, list):
                tg.allowed_user_ids = [str(x).strip() for x in raw if str(x).strip()]
            else:
                tg.allowed_user_ids = _parse_id_list(str(raw or ""))

    @staticmethod
    def _ensure_email_accounts(cfg: Config) -> None:
        if not cfg.email_accounts.profiles:
            cfg.email_accounts = _default_email_accounts()
        ids = {p.id for p in cfg.email_accounts.profiles}
        if cfg.email_accounts.default_id not in ids:
            cfg.email_accounts.default_id = cfg.email_accounts.profiles[0].id

    @staticmethod
    def _profile_from_dict(raw: dict, existing: EmailProfile | None = None) -> EmailProfile:
        base = existing or EmailProfile(
            id=str(raw.get("id") or "default"),
            name=str(raw.get("name") or "Default"),
        )
        pid = str(raw.get("id") or base.id or "default")
        name = str(raw.get("name") or base.name or "Default")
        profile = EmailProfile(id=pid, name=name)
        for field_name in (
            "smtp_host",
            "smtp_user",
            "smtp_password",
            "from_address",
            "imap_host",
            "imap_user",
            "imap_password",
        ):
            if field_name in raw:
                setattr(profile, field_name, str(raw.get(field_name) or ""))
            else:
                setattr(profile, field_name, getattr(base, field_name))
        for field_name, default in (("smtp_port", 587), ("imap_port", 993)):
            if field_name in raw:
                try:
                    setattr(profile, field_name, int(raw[field_name]))
                except (TypeError, ValueError):
                    setattr(profile, field_name, getattr(base, field_name, default))
            else:
                setattr(profile, field_name, getattr(base, field_name, default))
        return profile

    @staticmethod
    def _apply_email_accounts(cfg: Config, value: dict) -> None:
        if "default_id" in value and value["default_id"] is not None:
            cfg.email_accounts.default_id = str(value["default_id"])
        raw_profiles = value.get("profiles")
        if not isinstance(raw_profiles, list):
            return
        existing = {p.id: p for p in cfg.email_accounts.profiles}
        profiles: list[EmailProfile] = []
        seen: set[str] = set()
        for raw in raw_profiles:
            if not isinstance(raw, dict):
                continue
            pid = str(raw.get("id") or "").strip() or f"profile-{len(profiles) + 1}"
            if pid in seen:
                pid = f"{pid}-{len(profiles) + 1}"
            seen.add(pid)
            raw = {**raw, "id": pid}
            profiles.append(ConfigStore._profile_from_dict(raw, existing.get(pid)))
        if profiles:
            cfg.email_accounts.profiles = profiles

    @staticmethod
    def _apply_legacy_email(cfg: Config, value: dict) -> None:
        """Migrate flat `email` settings into the default profile."""
        if not cfg.email_accounts.profiles:
            cfg.email_accounts.profiles = [EmailProfile(id="default", name="Default")]
            cfg.email_accounts.default_id = "default"
        default_id = cfg.email_accounts.default_id or cfg.email_accounts.profiles[0].id
        updated: list[EmailProfile] = []
        found = False
        for p in cfg.email_accounts.profiles:
            if p.id == default_id:
                updated.append(ConfigStore._profile_from_dict({**value, "id": p.id, "name": p.name}, p))
                found = True
            else:
                updated.append(p)
        if not found:
            updated.insert(0, ConfigStore._profile_from_dict({**value, "id": "default", "name": "Default"}))
            cfg.email_accounts.default_id = "default"
        cfg.email_accounts.profiles = updated

    def get(self) -> Config:
        with self._lock:
            return self._config

    def update(self, data: dict) -> Config:
        with self._lock:
            self._apply(self._config, data)
            self._save()
            return self._config

    def _save(self) -> None:
        CONFIG_PATH.write_text(json.dumps(self.as_dict_unlocked(), indent=2))

    def as_dict_unlocked(self) -> dict:
        data = asdict(self._config)
        # Expose default profile under `email` for older clients / tooling.
        data["email"] = asdict(self._config.get_email_profile())
        return data

    def as_dict(self) -> dict:
        with self._lock:
            return self.as_dict_unlocked()


store = ConfigStore()
