"""Email tools: multi-profile SMTP send (gated) and IMAP read."""

from __future__ import annotations

import asyncio
import email
import imaplib
import smtplib
from email.message import EmailMessage

from ..config import Config, EmailProfile
from .base import Tool, ToolContext, ToolResult, prop


def _resolve_profile(cfg: Config, args: dict) -> EmailProfile:
    ref = args.get("profile") or args.get("account") or args.get("profile_id")
    return cfg.get_email_profile(str(ref) if ref else None)


def _profile_label(profile: EmailProfile) -> str:
    return f"{profile.name} ({profile.id})"


def _send_sync(profile: EmailProfile, to: str, subject: str, body: str) -> ToolResult:
    if not profile.smtp_host or not profile.from_address:
        return ToolResult(
            False,
            f"SMTP is not configured for profile '{_profile_label(profile)}'. "
            "Add SMTP settings under Configuration → Email profiles.",
        )
    msg = EmailMessage()
    msg["From"] = profile.from_address
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(profile.smtp_host, profile.smtp_port, timeout=30) as server:
            server.starttls()
            if profile.smtp_user:
                server.login(profile.smtp_user, profile.smtp_password)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        return ToolResult(False, f"Failed to send email via {_profile_label(profile)}: {exc}")
    return ToolResult(
        True,
        f"Email sent via {_profile_label(profile)} from {profile.from_address} "
        f"to {to} with subject '{subject}'.",
    )


async def _send(args: dict, ctx: ToolContext) -> ToolResult:
    profile = _resolve_profile(ctx.config, args)
    return await asyncio.to_thread(
        _send_sync, profile, args["to"], args.get("subject", ""), args.get("body", "")
    )


def _read_sync(profile: EmailProfile, folder: str, limit: int) -> ToolResult:
    if not profile.imap_host or not profile.imap_user:
        return ToolResult(
            False,
            f"IMAP is not configured for profile '{_profile_label(profile)}'. "
            "Add IMAP settings under Configuration → Email profiles.",
        )
    try:
        with imaplib.IMAP4_SSL(profile.imap_host, profile.imap_port) as mail:
            mail.login(profile.imap_user, profile.imap_password)
            mail.select(folder)
            _, data = mail.search(None, "ALL")
            ids = data[0].split()[-limit:]
            summaries = []
            for msg_id in reversed(ids):
                _, msg_data = mail.fetch(msg_id, "(RFC822)")
                raw = msg_data[0][1]
                message = email.message_from_bytes(raw)
                summaries.append(
                    f"From: {message.get('From')}\nSubject: {message.get('Subject')}\nDate: {message.get('Date')}"
                )
    except (imaplib.IMAP4.error, OSError) as exc:
        return ToolResult(False, f"Failed to read email via {_profile_label(profile)}: {exc}")
    if not summaries:
        return ToolResult(True, f"No messages in {folder} for {_profile_label(profile)}.")
    return ToolResult(
        True,
        f"Latest {len(summaries)} messages in {folder} "
        f"({_profile_label(profile)}):\n\n" + "\n\n".join(summaries),
    )


async def _read(args: dict, ctx: ToolContext) -> ToolResult:
    profile = _resolve_profile(ctx.config, args)
    return await asyncio.to_thread(
        _read_sync, profile, args.get("folder", "INBOX"), int(args.get("limit", 5))
    )


async def _list_profiles(args: dict, ctx: ToolContext) -> ToolResult:
    accounts = ctx.config.email_accounts
    profiles = accounts.profiles
    if not profiles:
        return ToolResult(True, "No email profiles configured.")
    lines = []
    for p in profiles:
        default = " [default]" if p.id == accounts.default_id else ""
        smtp = "smtp✓" if p.smtp_host and p.from_address else "smtp✗"
        imap = "imap✓" if p.imap_host and p.imap_user else "imap✗"
        lines.append(
            f"- id={p.id} name={p.name}{default} from={p.from_address or '(unset)'} ({smtp}, {imap})"
        )
    return ToolResult(True, "Email profiles:\n" + "\n".join(lines))


def _send_preview(args: dict) -> str:
    profile = args.get("profile") or args.get("account") or "default"
    return (
        f"Profile: {profile}\n"
        f"Send email to {args.get('to', '')} | Subject: {args.get('subject', '')}\n\n"
        f"{args.get('body', '')}"
    )


_PROFILE_PARAM = prop(
    "string",
    "Email profile id or name (optional; uses the default profile when omitted).",
    optional=True,
)

send_email = Tool(
    name="send_email",
    description=(
        "Send an email via a configured SMTP profile. "
        "Use list_email_profiles to see accounts; pass profile id/name when not using the default."
    ),
    parameters={
        "to": prop("string", "Recipient email address."),
        "subject": prop("string", "Email subject."),
        "body": prop("string", "Email body (plain text)."),
        "profile": _PROFILE_PARAM,
    },
    run=_send,
    dangerous=True,
    preview=_send_preview,
)

read_email = Tool(
    name="read_email",
    description=(
        "Read recent email message summaries via a configured IMAP profile. "
        "Use list_email_profiles to see accounts; pass profile id/name when not using the default."
    ),
    parameters={
        "folder": prop("string", "Mailbox folder (default INBOX).", optional=True),
        "limit": prop("integer", "How many recent messages (default 5).", optional=True),
        "profile": _PROFILE_PARAM,
    },
    run=_read,
)

list_email_profiles = Tool(
    name="list_email_profiles",
    description="List configured email profiles (id, name, default, SMTP/IMAP readiness).",
    parameters={},
    run=_list_profiles,
)
