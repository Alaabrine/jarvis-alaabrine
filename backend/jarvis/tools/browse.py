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
        "LIVE internet access — you are online. mode=search runs a real web search now "
        "and returns current titles, URLs and snippets; mode=fetch downloads a page and "
        "returns its text. Use this for anything you do not already know or that changes "
        "over time: today's news, weather, prices, scores, release notes, documentation, "
        "an unfamiliar error. Call it and answer from what comes back. You have no "
        "knowledge cutoff problem and no browsing restriction here, so never say you "
        "cannot access current information, never ask the user to look something up, and "
        "never offer to open news sites for them to read instead — read them yourself. "
        "Search snippets are often just a site's description, not its content: when you "
        "need the actual facts (today's headlines, the current price, what a page says), "
        "follow up with mode=fetch on the best two or three URLs and answer from the "
        "text you get back. If a page is blocked or empty, fetch a different source "
        "rather than giving up."
    ),
    parameters={
        "mode": prop("string", "'search' (default, live web search) or 'fetch' (read a URL).", optional=True),
        "query": prop("string", "Search query (mode=search).", optional=True),
        "url": prop("string", "URL to fetch (mode=fetch).", optional=True),
        "limit": prop("integer", "Max search results (default 6).", optional=True),
        "max_chars": prop("integer", "Max characters when fetching (default 6000).", optional=True),
    },
    run=_run,
)
