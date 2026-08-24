"""Unified email communication."""

from __future__ import annotations

from .base import Tool, ToolContext, ToolResult, prop
from . import email_tool


async def _run(args: dict, ctx: ToolContext) -> ToolResult:
    mode = (args.get("mode") or "send").strip().lower()
    if mode == "list_profiles":
        return await email_tool._list_profiles({}, ctx)
    if mode == "read":
        return await email_tool._read(
            {
                "profile": args.get("profile"),
                "folder": args.get("folder", "INBOX"),
                "limit": args.get("limit", 10),
            },
            ctx,
        )
    # send
    to = (args.get("to") or "").strip()
    subject = (args.get("subject") or "").strip()
    body = args.get("body") or ""
    if not to or not subject:
        return ToolResult(False, "send mode requires to and subject.")
    return await email_tool._send(
        {
            "to": to,
            "subject": subject,
            "body": body,
            "profile": args.get("profile"),
        },
        ctx,
    )


def _dangerous(args: dict) -> bool:
    return (args.get("mode") or "send").strip().lower() == "send"


communicate = Tool(
    name="communicate",
    description=(
        "Send or read email. mode=send (requires to, subject, body), "
        "mode=read (optional folder, limit), mode=list_profiles."
    ),
    parameters={
        "mode": prop("string", "'send' (default), 'read', or 'list_profiles'.", optional=True),
        "to": prop("string", "Recipient email (mode=send).", optional=True),
        "subject": prop("string", "Email subject (mode=send).", optional=True),
        "body": prop("string", "Email body (mode=send).", optional=True),
        "profile": prop("string", "Email profile id or name.", optional=True),
        "folder": prop("string", "IMAP folder (mode=read, default INBOX).", optional=True),
        "limit": prop("integer", "Max messages (mode=read).", optional=True),
    },
    run=_run,
    dangerous=_dangerous,
    preview=lambda a: (
        f"Send email to {a.get('to') or '?'}: {a.get('subject') or '(no subject)'}"
        if (a.get("mode") or "send").strip().lower() == "send"
        else f"Email ({a.get('mode', 'send')}): {a.get('subject') or a.get('folder') or ''}"
    ),
)
