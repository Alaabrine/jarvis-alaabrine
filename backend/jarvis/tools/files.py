"""File system tools. Reads and new-file writes run immediately; overwrite, move, and delete require confirmation."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .base import Tool, ToolContext, ToolResult, prop

MAX_READ = 200_000


def _expand(path: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(path))).resolve()


async def _read_file(args: dict, ctx: ToolContext) -> ToolResult:
    path = _expand(args["path"])
    if not path.exists():
        return ToolResult(False, f"No such file: {path}")
    if path.is_dir():
        return ToolResult(False, f"{path} is a directory. Use list_dir.")
    try:
        data = path.read_text(errors="replace")[:MAX_READ]
    except OSError as exc:
        return ToolResult(False, f"Could not read {path}: {exc}")
    return ToolResult(True, f"Contents of {path}:\n{data}")


async def _list_dir(args: dict, ctx: ToolContext) -> ToolResult:
    path = _expand(args.get("path", "."))
    if not path.exists():
        return ToolResult(False, f"No such directory: {path}")
    if not path.is_dir():
        return ToolResult(False, f"{path} is not a directory.")
    entries = []
    for entry in sorted(path.iterdir()):
        kind = "dir " if entry.is_dir() else "file"
        try:
            size = entry.stat().st_size
        except OSError:
            size = 0
        entries.append(f"{kind}  {size:>12}  {entry.name}")
    listing = "\n".join(entries) if entries else "(empty)"
    return ToolResult(True, f"{path}:\n{listing}")


async def _write_file(args: dict, ctx: ToolContext) -> ToolResult:
    path = _expand(args["path"])
    content = args.get("content", "")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    except OSError as exc:
        return ToolResult(False, f"Could not write {path}: {exc}")
    return ToolResult(True, f"Wrote {len(content)} characters to {path}.")


async def _move(args: dict, ctx: ToolContext) -> ToolResult:
    src = _expand(args["source"])
    dst = _expand(args["destination"])
    if not src.exists():
        return ToolResult(False, f"No such path: {src}")
    try:
        shutil.move(str(src), str(dst))
    except OSError as exc:
        return ToolResult(False, f"Could not move {src} -> {dst}: {exc}")
    return ToolResult(True, f"Moved {src} -> {dst}.")


async def _delete(args: dict, ctx: ToolContext) -> ToolResult:
    path = _expand(args["path"])
    if not path.exists():
        return ToolResult(False, f"No such path: {path}")
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError as exc:
        return ToolResult(False, f"Could not delete {path}: {exc}")
    return ToolResult(True, f"Deleted {path}.")


def _write_dangerous(args: dict) -> bool:
    # Overwriting an existing file is destructive; creating a new one is not.
    return _expand(args.get("path", "")).exists()


read_file = Tool(
    name="read_file",
    description="Read the contents of a text file.",
    parameters={"path": prop("string", "Absolute or ~-relative path to the file.")},
    run=_read_file,
)

list_dir = Tool(
    name="list_dir",
    description="List the contents of a directory.",
    parameters={"path": prop("string", "Directory path (default current directory).", optional=True)},
    run=_list_dir,
)

write_file = Tool(
    name="write_file",
    description="Write (create or overwrite) a text file with the given content.",
    parameters={
        "path": prop("string", "Path to write to."),
        "content": prop("string", "The full text content to write."),
    },
    run=_write_file,
    dangerous=_write_dangerous,
    preview=lambda a: f"Overwrite {_expand(a['path'])} with {len(a.get('content',''))} characters",
)

move_path = Tool(
    name="move_path",
    description="Move or rename a file or directory.",
    parameters={
        "source": prop("string", "Source path."),
        "destination": prop("string", "Destination path."),
    },
    run=_move,
    dangerous=True,
    preview=lambda a: f"Move {_expand(a['source'])} -> {_expand(a['destination'])}",
)

delete_path = Tool(
    name="delete_path",
    description="Delete a file or directory (recursively for directories).",
    parameters={"path": prop("string", "Path to delete.")},
    run=_delete,
    dangerous=True,
    preview=lambda a: f"Delete {_expand(a['path'])}",
)
