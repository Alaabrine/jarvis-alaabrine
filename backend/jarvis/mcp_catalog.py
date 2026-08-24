"""Discover MCP servers from public registries and the MCP.so sitemap."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from .config import McpServerEntry, store

log = logging.getLogger("jarvis.mcp_catalog")

_UA = "Mozilla/5.0 (X11; Linux x86_64) JARVIS/0.1 (+local-agent)"
_OFFICIAL = "https://registry.modelcontextprotocol.io/v0/servers"
_SMITHERY = "https://registry.smithery.ai/servers"
_MCPSO_SITEMAP = "https://mcp.so/sitemap.xml?section=servers&page=1"

_ID_SAFE = re.compile(r"[^a-zA-Z0-9._/-]+")


@dataclass
class CatalogEntry:
    id: str
    source: str
    name: str
    title: str
    description: str
    url: str = ""
    profile_url: str = ""
    repository: str = ""
    website_url: str = ""
    icon_url: str = ""
    version: str = ""
    requires_auth: bool = False
    auth_hint: str = ""
    remote: bool = True
    tool_count: int = 0
    tool_hints: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    # Registry package specs — how to run this server locally when it has no URL.
    packages: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self, *, installed: bool = False, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        data = asdict(self)
        data["installed"] = installed
        if extra:
            data.update(extra)
        return data


def _is_installed(url: str, *, identifier: str = "") -> bool:
    target = (url or "").strip().rstrip("/")
    needle = (identifier or "").strip().lower()
    for entry in store.get().mcp.servers:
        if target and entry.url.strip().rstrip("/") == target:
            return True
        if needle and needle in " ".join(entry.args or []).lower():
            return True
    return False


def _entry_id(source: str, key: str) -> str:
    key = _ID_SAFE.sub("-", key.strip())[:120]
    return f"{source}:{key}"


def _host_from_url(url: str) -> str:
    if not url:
        return ""
    try:
        host = urlparse(url).netloc.split(":")[0].lower()
        return host.removeprefix("www.")
    except Exception:  # noqa: BLE001
        return ""


def _icon_from_github(repository: str) -> str:
    match = re.match(r"https?://github\.com/([^/]+)/?", repository.strip(), re.I)
    if match:
        return f"https://github.com/{match.group(1)}.png?size=64"
    return ""


def _favicon_for_host(host: str) -> str:
    if not host or host in {"localhost", "127.0.0.1"}:
        return ""
    return f"https://icons.duckduckgo.com/ip3/{host}.ico"


def _resolve_icon(
    *,
    icon_url: str = "",
    website_url: str = "",
    repository: str = "",
    mcp_url: str = "",
) -> str:
    if icon_url.strip():
        return icon_url.strip()
    github = _icon_from_github(repository)
    if github:
        return github
    for candidate in (website_url, mcp_url):
        host = _host_from_url(candidate)
        favicon = _favicon_for_host(host)
        if favicon:
            return favicon
    return ""


def _apply_icon(entry: CatalogEntry) -> None:
    if not entry.icon_url:
        entry.icon_url = _resolve_icon(
            icon_url=entry.icon_url,
            website_url=entry.website_url,
            repository=entry.repository,
            mcp_url=entry.url,
        )


def _pick_remote_url(remotes: list[dict[str, Any]] | None) -> tuple[str, bool, str]:
    if not remotes:
        return "", False, ""
    preferred: dict[str, Any] | None = None
    for remote in remotes:
        if not isinstance(remote, dict):
            continue
        rtype = str(remote.get("type") or "").lower()
        if rtype in {"streamable-http", "http", "sse"}:
            preferred = remote
            break
    if preferred is None:
        preferred = remotes[0] if isinstance(remotes[0], dict) else None
    if not preferred:
        return "", False, ""
    url = str(preferred.get("url") or "").strip()
    headers = preferred.get("headers") or []
    requires_auth = False
    hint = ""
    if isinstance(headers, list):
        for header in headers:
            if not isinstance(header, dict):
                continue
            if header.get("isRequired") or header.get("isSecret"):
                requires_auth = True
            name = str(header.get("name") or "")
            desc = str(header.get("description") or header.get("value") or "")
            if name.lower() == "authorization" or "api key" in desc.lower():
                requires_auth = True
                hint = desc or "Authorization header required"
    return url, requires_auth, hint


def _smithery_mcp_url(data: dict[str, Any]) -> str:
    qn = str(data.get("qualifiedName") or "").strip()
    if qn:
        return f"https://server.smithery.ai/@{qn}/mcp"
    deployment = str(data.get("deploymentUrl") or "").strip()
    if deployment:
        return deployment if deployment.endswith("/mcp") else deployment.rstrip("/") + "/mcp"
    for conn in data.get("connections") or []:
        if isinstance(conn, dict):
            dep = str(conn.get("deploymentUrl") or "").strip()
            if dep:
                return dep if dep.endswith("/mcp") else dep.rstrip("/") + "/mcp"
    return ""


async def _fetch_official(query: str, limit: int) -> list[CatalogEntry]:
    params: dict[str, Any] = {"version": "latest", "limit": limit}
    if query.strip():
        params["search"] = query.strip()
    out: list[CatalogEntry] = []
    async with httpx.AsyncClient(timeout=25, headers={"User-Agent": _UA}) as client:
        resp = await client.get(_OFFICIAL, params=params)
        resp.raise_for_status()
        payload = resp.json()
    for item in payload.get("servers") or []:
        server = item.get("server") or {}
        name = str(server.get("name") or "")
        if not name:
            continue
        url, requires_auth, auth_hint = _pick_remote_url(server.get("remotes"))
        repo = server.get("repository") or {}
        repository = str(repo.get("url") or "") if isinstance(repo, dict) else ""
        entry = CatalogEntry(
            id=_entry_id("official", name),
            source="official",
            name=name,
            title=str(server.get("title") or name),
            description=str(server.get("description") or ""),
            url=url,
            profile_url=f"https://registry.modelcontextprotocol.io/v0/servers/{quote(name, safe='')}/versions/latest",
            repository=repository,
            website_url=str(server.get("websiteUrl") or ""),
            version=str(server.get("version") or ""),
            packages=[p for p in (server.get("packages") or []) if isinstance(p, dict)],
            requires_auth=requires_auth,
            auth_hint=auth_hint,
            remote=bool(url),
        )
        icons = server.get("icons") or []
        if isinstance(icons, list) and icons:
            first = icons[0]
            if isinstance(first, dict) and first.get("src"):
                entry.icon_url = str(first["src"])
        _apply_icon(entry)
        out.append(entry)
    return out


async def _fetch_smithery(query: str, limit: int) -> list[CatalogEntry]:
    params: dict[str, Any] = {"pageSize": min(limit, 50)}
    if query.strip():
        params["q"] = query.strip()
    out: list[CatalogEntry] = []
    async with httpx.AsyncClient(timeout=25, headers={"User-Agent": _UA}) as client:
        resp = await client.get(_SMITHERY, params=params)
        resp.raise_for_status()
        payload = resp.json()
    names_for_detail: list[str] = []
    base_entries: list[CatalogEntry] = []
    for item in payload.get("servers") or []:
        if not isinstance(item, dict):
            continue
        qn = str(item.get("qualifiedName") or "")
        if not qn:
            continue
        url = _smithery_mcp_url(item)
        requires_auth = bool(item.get("security")) or "server.smithery.ai/@" in url
        entry = CatalogEntry(
            id=_entry_id("smithery", qn),
            source="smithery",
            name=qn,
            title=str(item.get("displayName") or qn),
            description=str(item.get("description") or ""),
            url=url,
            profile_url=str(item.get("homepage") or f"https://smithery.ai/servers/{qn}"),
            website_url=str(item.get("homepage") or ""),
            icon_url=str(item.get("iconUrl") or ""),
            repository="",
            requires_auth=requires_auth,
            auth_hint="Smithery API key may be required (Authorization: Bearer …)" if requires_auth else "",
            remote=bool(item.get("remote", True)),
            tags=["verified"] if item.get("verified") else [],
        )
        _apply_icon(entry)
        base_entries.append(entry)
        if len(names_for_detail) < min(limit, 8):
            names_for_detail.append(qn)

    if names_for_detail:
        async with httpx.AsyncClient(timeout=25, headers={"User-Agent": _UA}) as client:
            details = await asyncio.gather(
                *[
                    _smithery_detail(client, qn)
                    for qn in names_for_detail
                ],
                return_exceptions=True,
            )
        detail_map = {
            qn: d for qn, d in zip(names_for_detail, details, strict=True) if isinstance(d, dict)
        }
        for entry in base_entries:
            detail = detail_map.get(entry.name)
            if not detail:
                continue
            alt_url = _smithery_mcp_url(detail)
            if alt_url:
                entry.url = alt_url
            if detail.get("iconUrl"):
                entry.icon_url = str(detail["iconUrl"])
            _apply_icon(entry)
            tools = detail.get("tools") or []
            if isinstance(tools, list):
                entry.tool_count = len(tools)
                entry.tool_hints = [
                    str(t.get("name") or "")
                    for t in tools
                    if isinstance(t, dict) and t.get("name")
                ][:8]
    out.extend(base_entries)
    return out


async def _smithery_detail(client: httpx.AsyncClient, qualified_name: str) -> dict[str, Any]:
    ns, _, slug = qualified_name.partition("/")
    if not slug:
        return {}
    resp = await client.get(f"{_SMITHERY}/{ns}/{slug}")
    resp.raise_for_status()
    return resp.json()


async def _fetch_mcpso(query: str, limit: int) -> list[CatalogEntry]:
    """Scrape MCP.so server slugs from the public sitemap and page metadata."""
    if not query.strip():
        return []
    q = query.strip().lower()
    out: list[CatalogEntry] = []
    try:
        async with httpx.AsyncClient(timeout=25, headers={"User-Agent": _UA}, follow_redirects=True) as client:
            resp = await client.get(_MCPSO_SITEMAP)
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
            ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            locs = [
                el.text.strip()
                for el in root.findall(".//sm:loc", ns)
                if el.text and "/servers/" in el.text
            ]
            matches = [loc for loc in locs if q in loc.lower()][:limit]
            metas = await asyncio.gather(
                *[_mcpso_page_meta(client, loc) for loc in matches],
                return_exceptions=True,
            )
    except Exception as exc:  # noqa: BLE001
        log.debug("MCP.so sitemap scrape failed: %s", exc)
        return []

    for loc, meta in zip(matches, metas, strict=True):
        if not isinstance(meta, dict):
            continue
        slug = loc.rstrip("/").split("/")[-1]
        out.append(
            CatalogEntry(
                id=_entry_id("mcpso", slug),
                source="mcpso",
                name=slug,
                title=str(meta.get("title") or slug),
                description=str(meta.get("description") or ""),
                url=str(meta.get("url") or ""),
                profile_url=loc,
                repository=str(meta.get("repository") or ""),
                website_url=str(meta.get("website_url") or ""),
                icon_url=str(meta.get("icon_url") or ""),
                requires_auth=False,
                remote=bool(meta.get("url")),
                tags=["mcpso"],
            )
        )
    for entry in out:
        _apply_icon(entry)
    return out


async def _mcpso_page_meta(client: httpx.AsyncClient, page_url: str) -> dict[str, str]:
    from bs4 import BeautifulSoup

    resp = await client.get(page_url)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    title = ""
    description = ""
    icon_url = ""
    for tag in soup.find_all("script", type="application/ld+json"):
        if not tag.string:
            continue
        try:
            data = json.loads(tag.string)
        except json.JSONDecodeError:
            continue
        if data.get("@type") == "SoftwareApplication":
            title = str(data.get("name") or title)
            description = str(data.get("description") or description)
            if data.get("image"):
                icon_url = str(data["image"])
    # Try to resolve an install URL via official registry using the slug tokens.
    slug = page_url.rstrip("/").split("/")[-1]
    resolved_url = await _resolve_url_from_slug(slug)
    return {
        "title": title or slug.replace("-", " ").title(),
        "description": description,
        "url": resolved_url,
        "repository": "",
        "icon_url": icon_url,
        "website_url": page_url,
    }


async def _resolve_url_from_slug(slug: str) -> str:
    tokens = [t for t in re.split(r"[-_]+", slug.lower()) if len(t) > 2][:3]
    if not tokens:
        return ""
    try:
        items = await _fetch_official(tokens[0], 5)
    except Exception:  # noqa: BLE001
        return ""
    slug_l = slug.lower()
    for item in items:
        if any(tok in item.name.lower() or tok in item.title.lower() for tok in tokens):
            if item.url:
                return item.url
        if slug_l.replace("-", "") in item.name.lower().replace(".", "").replace("/", ""):
            return item.url
    return items[0].url if items else ""


def _dedupe(entries: list[CatalogEntry]) -> list[CatalogEntry]:
    seen: set[str] = set()
    out: list[CatalogEntry] = []
    for entry in entries:
        key = entry.url.strip().rstrip("/") if entry.url else entry.id
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


async def search_catalog(
    query: str = "",
    *,
    limit: int = 24,
    sources: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Search public MCP directories; returns serializable entries."""
    src = {s.lower() for s in (sources or ["official", "smithery", "mcpso"])}
    tasks: list[asyncio.Task] = []
    if "official" in src:
        tasks.append(asyncio.create_task(_fetch_official(query, limit)))
    if "smithery" in src:
        tasks.append(asyncio.create_task(_fetch_smithery(query, limit)))
    if "mcpso" in src:
        tasks.append(asyncio.create_task(_fetch_mcpso(query, min(limit, 12))))

    merged: list[CatalogEntry] = []
    for task in tasks:
        try:
            merged.extend(await task)
        except Exception as exc:  # noqa: BLE001
            log.warning("Catalog source failed: %s", exc)

    if not query.strip():
        merged = [e for e in merged if e.url or e.source != "mcpso"]

    merged = _dedupe(merged)
    merged.sort(key=lambda e: (0 if e.url and not e.requires_auth else 1, e.source, e.title.lower()))
    return [e.as_dict(installed=_is_installed(e.url)) for e in merged[:limit]]


async def get_catalog_entry(entry_id: str, *, probe_tools: bool = False) -> dict[str, Any] | None:
    source, _, key = entry_id.partition(":")
    if not key:
        return None
    extra: dict[str, Any] = {"tools_detail": [], "remotes": [], "packages": []}

    if source == "official":
        name = key
        async with httpx.AsyncClient(timeout=25, headers={"User-Agent": _UA}) as client:
            resp = await client.get(f"{_OFFICIAL}/{quote(name, safe='')}/versions/latest")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            payload = resp.json()
        server = payload.get("server") or payload
        url, requires_auth, auth_hint = _pick_remote_url(server.get("remotes"))
        repo = server.get("repository") or {}
        entry = CatalogEntry(
            id=entry_id,
            source="official",
            name=name,
            title=str(server.get("title") or name),
            description=str(server.get("description") or ""),
            url=url,
            profile_url=f"{_OFFICIAL}/{quote(name, safe='')}/versions/latest",
            repository=str(repo.get("url") or "") if isinstance(repo, dict) else "",
            website_url=str(server.get("websiteUrl") or ""),
            version=str(server.get("version") or ""),
            requires_auth=requires_auth,
            auth_hint=auth_hint,
            remote=bool(url),
        )
        icons = server.get("icons") or []
        if isinstance(icons, list) and icons:
            first = icons[0]
            if isinstance(first, dict) and first.get("src"):
                entry.icon_url = str(first["src"])
        _apply_icon(entry)
        extra["remotes"] = server.get("remotes") or []
        extra["packages"] = server.get("packages") or []
        meta = payload.get("_meta") or {}
        extra["registry_meta"] = meta.get("io.modelcontextprotocol.registry/official") or {}

    elif source == "smithery":
        qn = key
        ns, _, slug = qn.partition("/")
        async with httpx.AsyncClient(timeout=25, headers={"User-Agent": _UA}) as client:
            resp = await client.get(f"{_SMITHERY}/{ns}/{slug}")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            detail = resp.json()
        url = _smithery_mcp_url(detail)
        tools = detail.get("tools") or []
        tool_hints = [
            str(t.get("name") or "") for t in tools if isinstance(t, dict) and t.get("name")
        ]
        entry = CatalogEntry(
            id=entry_id,
            source="smithery",
            name=qn,
            title=str(detail.get("displayName") or qn),
            description=str(detail.get("description") or ""),
            url=url,
            profile_url=str(detail.get("homepage") or f"https://smithery.ai/servers/{qn}"),
            website_url=str(detail.get("homepage") or ""),
            icon_url=str(detail.get("iconUrl") or ""),
            requires_auth="server.smithery.ai/@" in url or bool(detail.get("security")),
            auth_hint=(
                "Smithery API key may be required (Authorization: Bearer …)"
                if "server.smithery.ai/@" in url
                else ""
            ),
            remote=True,
            tool_count=len(tool_hints),
            tool_hints=tool_hints[:12],
            tags=["verified"] if detail.get("verified") else [],
        )
        _apply_icon(entry)
        extra["tools_detail"] = [
            {
                "name": str(t.get("name") or ""),
                "description": str(t.get("description") or ""),
            }
            for t in tools
            if isinstance(t, dict) and t.get("name")
        ]
        extra["connections"] = detail.get("connections") or []
        extra["verified"] = bool(detail.get("verified"))
        extra["use_count"] = detail.get("useCount")

    elif source == "mcpso":
        slug = key
        profile_url = f"https://mcp.so/servers/{slug}"
        try:
            async with httpx.AsyncClient(timeout=25, headers={"User-Agent": _UA}, follow_redirects=True) as client:
                meta = await _mcpso_page_meta(client, profile_url)
        except Exception:  # noqa: BLE001
            return None
        entry = CatalogEntry(
            id=entry_id,
            source="mcpso",
            name=slug,
            title=str(meta.get("title") or slug),
            description=str(meta.get("description") or ""),
            url=str(meta.get("url") or ""),
            profile_url=profile_url,
            website_url=str(meta.get("website_url") or profile_url),
            icon_url=str(meta.get("icon_url") or ""),
            requires_auth=False,
            remote=bool(meta.get("url")),
            tags=["mcpso"],
        )
        _apply_icon(entry)
    else:
        return None

    if probe_tools and entry.url and not extra.get("tools_detail"):
        probed = await _probe_remote_tools(entry.url)
        if probed:
            extra["tools_detail"] = probed
            entry.tool_count = len(probed)
            entry.tool_hints = [t["name"] for t in probed][:12]

    return entry.as_dict(installed=_is_installed(entry.url), extra=extra)


async def _probe_remote_tools(url: str) -> list[dict[str, str]]:
    from mcp import Client

    try:
        async with Client(url.strip(), read_timeout_seconds=12.0) as client:
            result = await client.list_tools()
            tools = getattr(result, "tools", result) or []
            out: list[dict[str, str]] = []
            for item in tools:
                name = getattr(item, "name", None) or (
                    item.get("name") if isinstance(item, dict) else ""
                )
                if not name:
                    continue
                desc = getattr(item, "description", None) or (
                    item.get("description") if isinstance(item, dict) else ""
                ) or ""
                out.append({"name": str(name), "description": str(desc)})
            return out
    except Exception as exc:  # noqa: BLE001
        log.debug("Tool probe failed for %s: %s", url, exc)
        return []


# Runtime used to launch each registry package type as a local stdio server.
_RUNTIME_BY_REGISTRY: dict[str, str] = {
    "npm": "npx",
    "pypi": "uvx",
    "oci": "docker",
    "docker": "docker",
    "mcpb": "",
    "nuget": "dnx",
}


def _arg_values(raw: Any) -> list[str]:
    """Flatten a registry argument list into plain argv strings."""
    out: list[str] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, str):
            if item.strip():
                out.append(item)
            continue
        if not isinstance(item, dict):
            continue
        value = str(item.get("value") or item.get("default") or "").strip()
        name = str(item.get("name") or "").strip()
        if item.get("type") == "named" and name:
            out.append(name)
            if value:
                out.append(value)
        elif value:
            out.append(value)
    return out


def stdio_spec_from_packages(packages: Any) -> dict[str, Any] | None:
    """Turn a registry ``packages`` list into a launchable local stdio server spec.

    Most registry entries ship no remote URL at all — they are meant to be run as a
    subprocess. Deriving that spec is what lets JARVIS install such a server itself
    instead of reporting "no remote MCP URL available".
    """
    if not isinstance(packages, list):
        return None
    for pkg in packages:
        if not isinstance(pkg, dict):
            continue
        transport = pkg.get("transport") or {}
        ttype = str(transport.get("type") or "stdio").lower() if isinstance(transport, dict) else "stdio"
        if ttype not in {"stdio", ""}:
            continue
        registry = str(pkg.get("registryType") or pkg.get("registry_name") or "").lower()
        identifier = str(pkg.get("identifier") or pkg.get("name") or "").strip()
        if not identifier:
            continue
        version = str(pkg.get("version") or "").strip()
        runtime = str(pkg.get("runtimeHint") or "").strip() or _RUNTIME_BY_REGISTRY.get(registry, "")
        if not runtime:
            continue

        runtime_args = _arg_values(pkg.get("runtimeArguments"))
        package_args = _arg_values(pkg.get("packageArguments"))

        if registry == "npm" or runtime in {"npx", "bunx"}:
            command = runtime if runtime in {"npx", "bunx"} else "npx"
            args = list(runtime_args)
            if "-y" not in args and "--yes" not in args:
                args.insert(0, "-y")
            args.append(f"{identifier}@{version}" if version else identifier)
        elif registry == "pypi" or runtime in {"uvx", "uv"}:
            command = "uvx"
            args = [*runtime_args, f"{identifier}=={version}" if version else identifier]
        elif registry in {"oci", "docker"} or runtime == "docker":
            command = "docker"
            image = f"{identifier}:{version}" if version and ":" not in identifier else identifier
            args = ["run", "-i", "--rm", *runtime_args, image]
        else:
            command = runtime
            args = [*runtime_args, identifier]
        args.extend(package_args)

        required_env: list[str] = []
        optional_env: list[str] = []
        defaults: dict[str, str] = {}
        for var in pkg.get("environmentVariables") or []:
            if not isinstance(var, dict):
                continue
            name = str(var.get("name") or "").strip()
            if not name:
                continue
            default = str(var.get("default") or "").strip()
            if default:
                defaults[name] = default
            elif var.get("isRequired"):
                required_env.append(name)
            else:
                optional_env.append(name)

        return {
            "command": command,
            "args": [a for a in args if a],
            "registry": registry or "unknown",
            "identifier": identifier,
            "version": version,
            "env": defaults,
            "required_env": required_env,
            "optional_env": optional_env,
        }
    return None


def _slugify_id(name: str) -> str:
    base = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")[:40]
    return base or "server"


def _unique_server_id(preferred: str) -> str:
    server_id = _slugify_id(preferred)
    used = {s.id for s in store.get().mcp.servers}
    if server_id not in used:
        return server_id
    n = 2
    while f"{server_id}-{n}" in used:
        n += 1
    return f"{server_id}-{n}"


async def _add_server(entry: McpServerEntry) -> dict[str, Any]:
    """Persist a new server entry and reconnect so its tools import immediately."""
    from .mcp_client import get_manager

    cfg = store.get()
    servers = [*cfg.mcp.servers, entry]
    store.update({"mcp": {"enabled": cfg.mcp.enabled, "servers": [asdict(s) for s in servers]}})
    statuses = await get_manager().refresh()
    status = next((s for s in statuses if s.id == entry.id), None)
    return {
        "ok": bool(status and status.connected),
        "already_installed": False,
        "server_id": entry.id,
        "name": entry.name,
        "transport": entry.transport,
        "url": entry.url,
        "command": entry.target,
        "connected": bool(status and status.connected),
        "error": status.error if status else "",
        "tool_count": len(status.tools) if status else 0,
        "tools": status.tools if status else [],
    }


async def install_local_server(
    name: str,
    command: str,
    args: list[str] | None = None,
    *,
    env: dict[str, str] | None = None,
    cwd: str = "",
) -> dict[str, Any]:
    """Install a local (stdio) MCP server that JARVIS launches as a subprocess."""
    command = (command or "").strip()
    if not command:
        raise ValueError("command is required for a local MCP server")
    argv = [str(a) for a in (args or []) if str(a).strip()]

    signature = " ".join([command, *argv])
    for existing in store.get().mcp.servers:
        if existing.target == signature:
            return {
                "ok": True,
                "already_installed": True,
                "server_id": existing.id,
                "name": existing.name,
                "command": existing.target,
                "transport": "stdio",
            }

    entry = McpServerEntry(
        id=_unique_server_id(name or command),
        name=str(name or command)[:80],
        url="",
        enabled=True,
        transport="stdio",
        command=command,
        args=argv,
        env={k: str(v) for k, v in (env or {}).items() if k},
        cwd=cwd.strip(),
    )
    return await _add_server(entry)


async def install_catalog_entry(
    entry_id: str,
    *,
    url: str | None = None,
    authorization: str | None = None,
    name: str | None = None,
    env: dict[str, str] | None = None,
    prefer: str = "auto",
) -> dict[str, Any]:
    """Add a catalog server to JARVIS config and refresh imported tools.

    Falls back to running the server locally over stdio when it publishes no remote
    URL, which is the common case in the public registries.
    """
    meta = await get_catalog_entry(entry_id, probe_tools=False)
    if meta is None and not url:
        raise ValueError(f"Unknown catalog entry: {entry_id}")
    meta = meta or {}

    install_url = (url or meta.get("url") or "").strip()
    display_name = name or meta.get("title") or meta.get("name") or entry_id
    spec = stdio_spec_from_packages(meta.get("packages")) if prefer != "remote" else None

    if install_url and prefer != "local":
        parsed = urlparse(install_url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("Only http(s) MCP URLs can be installed")

        headers: dict[str, str] = {}
        auth = (authorization or "").strip()
        if auth:
            headers["Authorization"] = auth if auth.lower().startswith("bearer ") else f"Bearer {auth}"

        norm = install_url.rstrip("/")
        for existing in store.get().mcp.servers:
            if existing.url.strip().rstrip("/") == norm:
                return {
                    "ok": True,
                    "already_installed": True,
                    "server_id": existing.id,
                    "url": existing.url,
                    "name": existing.name,
                    "transport": "http",
                }

        entry = McpServerEntry(
            id=_unique_server_id(str(meta.get("name") or display_name)),
            name=str(display_name)[:80],
            url=install_url,
            enabled=True,
            headers=headers,
            transport="http",
        )
        result = await _add_server(entry)
        # A remote server that will not connect is worth retrying locally.
        if result.get("connected") or not spec:
            return result
        log.info("Remote install of %s failed (%s); trying local stdio", entry_id, result.get("error"))
        _remove_server_id(entry.id)

    if not spec:
        raise ValueError(
            "No remote MCP URL and no runnable package for this entry — "
            "install it manually with mode=install command=<executable> args=[…]"
        )

    runtime = spec["command"]
    if not _runtime_available(runtime):
        raise ValueError(
            f"This server runs locally via '{runtime}', which is not installed. "
            f"Install {runtime} first (device_control action=shell), then retry."
        )

    merged_env = {**spec.get("env", {}), **{k: str(v) for k, v in (env or {}).items() if k}}
    missing = [name_ for name_ in spec.get("required_env") or [] if name_ not in merged_env]

    result = await install_local_server(
        str(display_name),
        runtime,
        spec["args"],
        env=merged_env,
    )
    result["transport"] = "stdio"
    result["registry"] = spec.get("registry")
    if missing:
        result["missing_env"] = missing
        result["error"] = (
            (result.get("error") or "")
            + f" Required environment variables not set: {', '.join(missing)}."
        ).strip()
    return result


def _runtime_available(runtime: str) -> bool:
    import shutil

    return bool(shutil.which(runtime))


_GOAL_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "for", "to", "with", "from", "as", "in", "on",
    "my", "me", "i", "can", "you", "able", "use", "using", "get", "let", "so", "that",
    "this", "it", "its", "be", "is", "are", "do", "does", "need", "want", "please",
    "server", "mcp", "tool", "tools", "support", "integration", "access", "api",
}


def goal_keywords(goal: str) -> list[str]:
    """Distinctive words from a natural-language goal, in the order the user said them
    (earlier words usually name the subject: "roblox studio automation")."""
    words = [w for w in re.split(r"[^a-zA-Z0-9]+", (goal or "").lower()) if w]
    seen: set[str] = set()
    out: list[str] = []
    for word in words:
        if len(word) < 3 or word in _GOAL_STOPWORDS or word in seen:
            continue
        seen.add(word)
        out.append(word)
    return out


def goal_queries(goal: str) -> list[str]:
    """Search strings to try, from most specific to broadest.

    Registry search does poor-to-nothing with a full sentence, so a goal like
    "fetch and read a web page as markdown" also gets tried as "markdown fetch",
    then as its single strongest keyword.
    """
    goal = (goal or "").strip()
    keywords = goal_keywords(goal)
    queries: list[str] = []
    for candidate in (goal, " ".join(keywords[:3]), " ".join(keywords[:2]), *keywords[:2]):
        candidate = candidate.strip()
        if candidate and candidate not in queries:
            queries.append(candidate)
    return queries


# Verbs and nouns that appear in half the registry — matching only these means the
# candidate is not actually about the goal ("control" alone must not select an
# export-control server for "control spotify playback").
_GENERIC_GOAL_WORDS = {
    "control", "manage", "management", "read", "write", "query", "fetch", "get", "put",
    "send", "list", "search", "find", "create", "make", "update", "delete", "remove",
    "open", "close", "start", "stop", "run", "call", "check", "show", "view", "watch",
    "playback", "play", "data", "info", "information", "content", "text", "page",
    "web", "local", "remote", "client", "service", "system", "automation", "assistant",
    "agent", "generic", "helper", "utility", "connector", "bridge", "wrapper",
}


def specific_keywords(goal: str) -> list[str]:
    """Goal keywords that actually identify a subject (drops generic verbs)."""
    return [t for t in goal_keywords(goal) if t not in _GENERIC_GOAL_WORDS]


def _mentions_subject(item: dict[str, Any], specific: list[str]) -> bool:
    """Does this candidate name any of the goal's distinctive words?"""
    if not specific:
        return True
    haystack = " ".join(
        str(item.get(key) or "") for key in ("title", "name", "description", "id")
    ).lower()
    haystack += " " + " ".join(str(t) for t in (item.get("tool_hints") or [])).lower()
    return any(token in haystack for token in specific)


def _relevance(item: dict[str, Any], tokens: list[str]) -> int:
    """How many goal keywords this candidate's own text mentions."""
    if not tokens:
        return 0
    haystack = " ".join(
        str(item.get(key) or "")
        for key in ("title", "name", "description", "id")
    ).lower()
    haystack += " " + " ".join(str(t) for t in (item.get("tool_hints") or [])).lower()
    return sum(1 for token in tokens if token in haystack)


def _local_spec_state(item: dict[str, Any]) -> tuple[bool, bool]:
    spec = stdio_spec_from_packages(item.get("packages"))
    if not spec:
        return False, False
    ready = _runtime_available(spec["command"]) and not spec.get("required_env")
    return True, ready


def rank_key(tokens: list[str] | None = None):
    """Sort key ordering candidates by relevance, then by how likely they are to
    connect with no user input at all."""

    def key(item: dict[str, Any]) -> tuple:
        local_any, local_ready = _local_spec_state(item)
        has_remote = bool(item.get("url"))
        needs_auth = bool(item.get("requires_auth"))
        return (
            0 if item.get("installed") else 1,
            -_relevance(item, tokens or []),
            0 if (has_remote and not needs_auth) else 1,
            0 if local_ready else 1,
            0 if local_any else 1,
            0 if item.get("source") == "official" else 1,
            -int(item.get("tool_count") or 0),
        )

    return key



def _remove_server_id(server_id: str) -> bool:
    """Drop a server entry from config without reconnecting (used to roll back)."""
    cfg = store.get()
    keep = [e for e in cfg.mcp.servers if e.id != server_id]
    if len(keep) == len(cfg.mcp.servers):
        return False
    store.update({"mcp": {"enabled": cfg.mcp.enabled, "servers": [asdict(s) for s in keep]}})
    return True


async def ensure_capability(
    goal: str,
    *,
    max_attempts: int = 3,
    limit: int = 12,
    env: dict[str, str] | None = None,
    authorization: str | None = None,
) -> dict[str, Any]:
    """Find, install, and connect an MCP server that provides ``goal``.

    This is the autonomous path: given "control Spotify" or "query Postgres", search
    the registries under several phrasings, rank candidates by relevance and by how
    likely they are to work unattended, then install them in order until one connects.
    Entries that fail to connect are rolled back so config is not left with dead
    servers, and candidates that need an API key are reported so JARVIS can ask for it.
    """
    goal = (goal or "").strip()
    if not goal:
        raise ValueError("goal is required")

    from .mcp_client import get_manager

    mgr = get_manager()
    tokens = goal_keywords(goal)

    # An already-connected server may well cover this goal.
    for status in mgr.status_list():
        if not status.connected:
            continue
        haystack = (status.name + " " + " ".join(
            f"{t.get('name','')} {t.get('description','')}" for t in status.tools
        )).lower()
        hits = sum(1 for t in tokens if t in haystack)
        if tokens and hits >= max(1, len(tokens) // 2):
            return {
                "ok": True,
                "goal": goal,
                "already_available": True,
                "server_id": status.id,
                "name": status.name,
                "tool_count": len(status.tools),
                "tools": status.tools,
                "attempts": [],
            }

    # Search under several phrasings — a full sentence rarely matches a registry index.
    merged: dict[str, dict[str, Any]] = {}
    for query in goal_queries(goal)[:3]:
        try:
            items = await search_catalog(query, limit=limit)
        except Exception as exc:  # noqa: BLE001
            log.warning("Catalog search for %r failed: %s", query, exc)
            continue
        for item in items:
            key = str(item.get("id") or item.get("url") or "")
            if key and key not in merged:
                merged[key] = item
        if len(merged) >= limit * 2:
            break

    candidates = list(merged.values())
    if not candidates:
        return {
            "ok": False,
            "goal": goal,
            "error": f"No MCP server found for '{goal}'.",
            "attempts": [],
            "needs_auth": [],
        }

    candidates.sort(key=rank_key(tokens))

    # Reject candidates that only matched a generic verb — installing an unrelated
    # server is worse than reporting that nothing suitable was found.
    specific = specific_keywords(goal)
    on_subject = [c for c in candidates if _mentions_subject(c, specific)]
    if not on_subject:
        return {
            "ok": False,
            "goal": goal,
            "error": (
                f"No MCP server in the public registries is about '{' '.join(specific) or goal}'. "
                "Nothing was installed."
            ),
            "attempts": [],
            "needs_auth": [],
            "off_topic_candidates": [str(c.get("title") or c.get("name")) for c in candidates[:5]],
        }
    candidates = on_subject

    # Servers gated behind an API key cannot be provisioned unattended; surface them.
    auth = (authorization or "").strip()
    gated: list[dict[str, Any]] = []
    attemptable: list[dict[str, Any]] = []
    for item in candidates:
        local_any, _ = _local_spec_state(item)
        if item.get("requires_auth") and not auth and not local_any:
            gated.append(item)
        else:
            attemptable.append(item)

    attempts: list[dict[str, Any]] = []
    for item in attemptable[: max(1, max_attempts)]:
        entry_id = str(item.get("id") or "")
        try:
            result = await install_catalog_entry(entry_id, env=env, authorization=auth or None)
        except Exception as exc:  # noqa: BLE001
            attempts.append({"id": entry_id, "name": item.get("title"), "error": str(exc)})
            continue
        if result.get("connected") or result.get("already_installed"):
            result["goal"] = goal
            result["attempts"] = attempts
            return result
        attempts.append(
            {
                "id": entry_id,
                "name": result.get("name"),
                "connected": False,
                "transport": result.get("transport"),
                "error": result.get("error"),
                "missing_env": result.get("missing_env"),
            }
        )
        # Do not leave a server that will not connect sitting in the config.
        if result.get("server_id"):
            _remove_server_id(str(result["server_id"]))

    if attempts or gated:
        await mgr.refresh()

    needs_auth = [
        {
            "id": item.get("id"),
            "name": item.get("title") or item.get("name"),
            "auth_hint": item.get("auth_hint") or "API key or Authorization header required",
        }
        for item in gated[:4]
    ]
    error = f"Tried {len(attempts)} candidate server(s) for '{goal}'; none connected."
    if not attempts and needs_auth:
        error = (
            f"Every MCP server found for '{goal}' needs an API key. "
            "Ask for the key, then retry with authorization=<key>."
        )
    return {
        "ok": False,
        "goal": goal,
        "error": error,
        "attempts": attempts,
        "needs_auth": needs_auth,
    }


async def remove_server(ref: str) -> str:
    """Uninstall a configured MCP server by id or name."""
    from .mcp_client import get_manager

    needle = (ref or "").strip().lower()
    cfg = store.get()
    keep: list[McpServerEntry] = []
    removed = ""
    for entry in cfg.mcp.servers:
        if not removed and needle in {entry.id.lower(), entry.name.lower()}:
            removed = entry.name
            continue
        keep.append(entry)
    if not removed:
        partial = [
            e for e in cfg.mcp.servers
            if needle and (needle in e.name.lower() or needle in e.id.lower())
        ]
        if len(partial) != 1:
            raise ValueError(f"No installed MCP server matches '{ref}'.")
        removed = partial[0].name
        keep = [e for e in cfg.mcp.servers if e is not partial[0]]

    store.update({"mcp": {"enabled": cfg.mcp.enabled, "servers": [asdict(s) for s in keep]}})
    await get_manager().refresh()
    return removed
