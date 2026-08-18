"""Arbitrary shell command execution. Read-only commands run automatically; anything
that may modify the system requires confirmation (unless auto-approve is enabled).

When a sudo password is configured, ``sudo`` commands are rewritten to ``sudo -S`` and
the password is supplied on stdin (never logged or returned in tool output).
"""

from __future__ import annotations

import asyncio
import re
import shlex

from .base import Tool, ToolContext, ToolResult, prop

# Commands considered read-only / safe to run without confirmation.
_SAFE_COMMANDS = {
    "ls", "cat", "pwd", "whoami", "id", "date", "uptime", "df", "du", "free",
    "ps", "top", "env", "printenv", "uname", "hostname", "which", "echo",
    "head", "tail", "wc", "stat", "file", "grep", "rg", "find", "tree",
    "ip", "ifconfig", "netstat", "ss", "lscpu", "lsblk", "lsusb", "nvidia-smi",
    "git", "python", "python3", "node", "pip", "pip3",
}
# Even "safe" base commands are dangerous with these tokens present.
_DANGEROUS_TOKENS = {">", ">>", "rm", "mv", "dd", "mkfs", "sudo", "chmod", "chown", "kill",
                     "shutdown", "reboot", "|", "&", ";", "install", "uninstall"}

_SUDO_RE = re.compile(r"(^|[\s;|&])sudo(?=\s)")


def _is_dangerous(args: dict) -> bool:
    command = args.get("command", "")
    try:
        tokens = shlex.split(command)
    except ValueError:
        return True
    if not tokens:
        return True
    if any(tok in _DANGEROUS_TOKENS for tok in tokens):
        return True
    base = tokens[0].split("/")[-1]
    # git/pip/python are only "safe" for read operations; be conservative on subcommands.
    if base in {"git"} and len(tokens) > 1 and tokens[1] in {"push", "reset", "clean", "rebase", "commit", "checkout"}:
        return True
    if base in {"pip", "pip3"} and len(tokens) > 1 and tokens[1] in {"install", "uninstall"}:
        return True
    return base not in _SAFE_COMMANDS


def _uses_sudo(command: str) -> bool:
    return bool(_SUDO_RE.search(command))


def _with_sudo_stdin(command: str) -> str:
    """Ensure every ``sudo`` invocation reads the password from stdin without a prompt."""
    parts: list[str] = []
    i = 0
    for m in re.finditer(r"(^|[\s;|&])sudo(\s+)", command):
        parts.append(command[i:m.start()])
        prefix, space = m.group(1), m.group(2)
        rest = command[m.end():]
        if rest.startswith("-S") or rest.startswith("-n"):
            parts.append(m.group(0))
        else:
            parts.append(f"{prefix}sudo -S -p ''{space}")
        i = m.end()
    parts.append(command[i:])
    return "".join(parts) if parts else command


def _scrub_secrets(text: str, password: str) -> str:
    if not password or not text:
        return text
    return text.replace(password, "********")


async def _run(args: dict, ctx: ToolContext) -> ToolResult:
    command = args["command"]
    cwd = args.get("cwd") or None
    timeout = int(args.get("timeout", 60))
    password = (ctx.config.permissions.sudo_password or "").strip()
    display_cmd = command
    stdin_data: bytes | None = None

    if _uses_sudo(command):
        if not password:
            return ToolResult(
                False,
                "This command needs sudo, but no sudo password is configured. "
                "Add one under Configuration → Permissions, or run the command without sudo.",
            )
        command = _with_sudo_stdin(command)
        stdin_data = (password + "\n").encode()

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(input=stdin_data),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            return ToolResult(False, f"Command timed out after {timeout}s: {display_cmd}")
    except OSError as exc:
        return ToolResult(False, f"Failed to run command: {exc}")

    output = _scrub_secrets(stdout.decode(errors="replace")[:20_000], password)
    # sudo -S often echoes nothing useful on auth failure — surface a clear hint.
    if proc.returncode != 0 and password and _uses_sudo(display_cmd):
        low = output.lower()
        if "sorry" in low or "try again" in low or "incorrect password" in low or "auth" in low:
            output += "\n(Hint: sudo authentication may have failed — check the configured password.)"

    status = "succeeded" if proc.returncode == 0 else f"exited with code {proc.returncode}"
    return ToolResult(proc.returncode == 0, f"$ {display_cmd}\n({status})\n{output}")


run_shell = Tool(
    name="run_shell",
    description=(
        "Run a shell command on this device and return its output. Use for system tasks, "
        "launching processes, inspecting the machine, or anything not covered by other tools. "
        "If a sudo password is configured in settings, you may use sudo for privileged commands."
    ),
    parameters={
        "command": prop("string", "The shell command to execute."),
        "cwd": prop("string", "Working directory (optional).", optional=True),
        "timeout": prop("integer", "Timeout in seconds (default 60).", optional=True),
    },
    run=_run,
    dangerous=_is_dangerous,
    preview=lambda a: f"Run shell command: {a.get('command','')}",
)
