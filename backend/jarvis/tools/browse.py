"""Unified internet access — search and read pages in one tool."""

from __future__ import annotations

from .base import Tool, ToolContext, ToolResult, prop
from . import web


async def _run(args: dict, ctx: ToolContext) -> ToolResult:
    mode = (args.get("mode") or "search").strip().lower()
    if mode == "fetch":
        url = (args.get("url") or "").strip()
        if not url:
            return ToolResult(False, "fetch mode requires url.")
        return await web._fetch(
            {"url": url, "max_chars": args.get("max_chars", 6000)},
            ctx,
        )
    query = (args.get("query") or args.get("url") or "").strip()
    if not query:
        return ToolResult(False, "search mode requires query.")
    return await web._search({"query": query, "limit": args.get("limit", 6)}, ctx)


browse = Tool(
    name="browse",
    description=(
        "Access the internet. mode=search to find information (returns titles, URLs, snippets); "
        "mode=fetch to read a page's text content. Use freely to learn anything you need."
    ),
    parameters={
        "mode": prop("string", "'search' (default) or 'fetch'.", optional=True),
        "query": prop("string", "Search query (mode=search).", optional=True),
        "url": prop("string", "URL to fetch (mode=fetch).", optional=True),
        "limit": prop("integer", "Max search results (default 6).", optional=True),
        "max_chars": prop("integer", "Max characters when fetching (default 6000).", optional=True),
    },
    run=_run,
)
