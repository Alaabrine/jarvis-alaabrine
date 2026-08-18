"""Internet access tools: web search and page reading."""

from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from .base import Tool, ToolContext, ToolResult, prop

_UA = "Mozilla/5.0 (X11; Linux x86_64) JARVIS/0.1 (+local-agent)"


def _unwrap_ddg(href: str) -> str:
    """DuckDuckGo wraps result links as //duckduckgo.com/l/?uddg=<real-url>. Unwrap it."""
    if "uddg=" in href:
        query = urlparse(href if href.startswith("http") else "https:" + href).query
        target = parse_qs(query).get("uddg")
        if target:
            return unquote(target[0])
    if href.startswith("//"):
        return "https:" + href
    return href


async def _search(args: dict, ctx: ToolContext) -> ToolResult:
    query = args["query"]
    limit = int(args.get("limit", 6))
    cfg = ctx.config

    # Prefer a SearXNG JSON endpoint when configured.
    if cfg.searxng_url:
        try:
            async with httpx.AsyncClient(timeout=20, headers={"User-Agent": _UA}) as client:
                resp = await client.get(
                    cfg.searxng_url.rstrip("/") + "/search",
                    params={"q": query, "format": "json"},
                )
                resp.raise_for_status()
                data = resp.json()
                results = data.get("results", [])[:limit]
                lines = [
                    f"- {r.get('title','').strip()}\n  {r.get('url','')}\n  {r.get('content','').strip()}"
                    for r in results
                ]
                if lines:
                    return ToolResult(True, f"Search results for '{query}':\n" + "\n".join(lines))
        except (httpx.HTTPError, ValueError):
            pass  # fall through to DuckDuckGo

    # Fallback: DuckDuckGo HTML endpoint.
    try:
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": _UA}, follow_redirects=True) as client:
            resp = await client.get("https://duckduckgo.com/html/", params={"q": query})
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        return ToolResult(False, f"Web search failed: {exc}")

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for item in soup.select(".result")[: limit * 2]:
        title_el = item.select_one(".result__a")
        snippet_el = item.select_one(".result__snippet")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        href = _unwrap_ddg(title_el.get("href", ""))
        snippet = snippet_el.get_text(strip=True) if snippet_el else ""
        results.append(f"- {title}\n  {href}\n  {snippet}")
        if len(results) >= limit:
            break

    if not results:
        return ToolResult(True, f"No results found for '{query}'.")
    return ToolResult(True, f"Search results for '{query}':\n" + "\n".join(results))


async def _fetch(args: dict, ctx: ToolContext) -> ToolResult:
    url = args["url"]
    max_chars = int(args.get("max_chars", 6000))
    try:
        async with httpx.AsyncClient(timeout=25, headers={"User-Agent": _UA}, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        return ToolResult(False, f"Failed to fetch {url}: {exc}")

    content_type = resp.headers.get("content-type", "")
    if "html" not in content_type and "xml" not in content_type:
        text = resp.text[:max_chars]
        return ToolResult(True, f"Content of {url} ({content_type}):\n{text}")

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        tag.decompose()
    title = soup.title.get_text(strip=True) if soup.title else ""
    text = " ".join(soup.get_text(" ", strip=True).split())
    text = text[:max_chars]
    return ToolResult(True, f"Page: {title}\nURL: {url}\n\n{text}")


web_search = Tool(
    name="web_search",
    description="Search the web and return a list of relevant results (title, URL, snippet).",
    parameters={
        "query": prop("string", "The search query."),
        "limit": prop("integer", "Maximum number of results (default 6).", optional=True),
    },
    run=_search,
)

web_fetch = Tool(
    name="web_fetch",
    description="Fetch a URL and return its readable text content. Use after web_search to read a page.",
    parameters={
        "url": prop("string", "The full URL to fetch."),
        "max_chars": prop("integer", "Maximum characters to return (default 6000).", optional=True),
    },
    run=_fetch,
)
