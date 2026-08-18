"""OpenAI-compatible LLM client with local-first (Odysseus) + cloud fallback."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from openai import APIConnectionError, APIError, AsyncOpenAI

from .config import LLMConfig

DeltaFn = Callable[[dict[str, Any]], Awaitable[None]]

_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)


class LLMError(RuntimeError):
    pass


class Interrupted(Exception):
    """Raised when a stream or agent run is cancelled by the user."""


@dataclass
class ChatMessage:
    """Normalized assistant message from any OpenAI-compatible endpoint."""

    content: str = ""
    reasoning: str = ""
    tool_calls: list[Any] = field(default_factory=list)


def _looks_complete_json(raw: str) -> bool:
    text = (raw or "").strip()
    if not text:
        return False
    try:
        json.loads(text)
        return True
    except json.JSONDecodeError:
        return False


def split_thinking(text: str) -> tuple[str, str]:
    """Split model output into (reasoning, visible_content)."""
    if not text:
        return "", ""
    parts = _THINK_RE.findall(text)
    visible = _THINK_RE.sub("", text).strip()
    lower = text.lower()
    if "<think>" in lower and "</think>" not in lower:
        idx = lower.find("<think>")
        body = text[idx + len("<think>") :]
        parts = list(parts) + [body]
        visible = text[:idx].strip()
    reasoning = "\n\n".join(p.strip() for p in parts if p.strip())
    return reasoning, visible


def _normalise_messages_for_vision(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure multimodal parts use the OpenAI ``image_url`` object shape."""
    out: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            out.append(msg)
            continue
        new_content: list[dict[str, Any]] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "image_url":
                image_url = part.get("image_url")
                url = ""
                if isinstance(image_url, str):
                    url = image_url
                elif isinstance(image_url, dict):
                    url = str(image_url.get("url") or "")
                if url:
                    if not url.startswith("data:") and not url.startswith("http"):
                        url = f"data:image/jpeg;base64,{url}"
                    new_content.append({"type": "image_url", "image_url": {"url": url}})
            else:
                new_content.append(part)
        out.append({**msg, "content": new_content})
    return out


class LLMClient:
    """Wraps two OpenAI-compatible endpoints and chooses between them."""

    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg

    def _primary(self) -> tuple[AsyncOpenAI, str] | None:
        if self.cfg.prefer == "cloud" and self.cfg.fallback_base_url:
            return self._cloud()
        return self._local()

    def _secondary(self) -> tuple[AsyncOpenAI, str] | None:
        if self.cfg.prefer == "cloud":
            return self._local()
        return self._cloud()

    def _local(self) -> tuple[AsyncOpenAI, str] | None:
        if not self.cfg.base_url:
            return None
        client = AsyncOpenAI(base_url=self.cfg.base_url, api_key=self.cfg.api_key or "local")
        return client, self.cfg.model

    def _cloud(self) -> tuple[AsyncOpenAI, str] | None:
        if not self.cfg.fallback_base_url:
            return None
        client = AsyncOpenAI(
            base_url=self.cfg.fallback_base_url,
            api_key=self.cfg.fallback_api_key or "cloud",
        )
        return client, self.cfg.fallback_model or self.cfg.model

    def _endpoints(self) -> list[tuple[AsyncOpenAI, str]]:
        return [ep for ep in (self._primary(), self._secondary()) if ep]

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        on_delta: DeltaFn | None = None,
        cancel: Any | None = None,
        tool_choice: Any | None = None,
    ) -> ChatMessage:
        """Stream a chat completion; ``on_delta`` gets live reasoning/content tokens.

        ``tool_choice`` overrides the default ``"auto"`` (e.g. ``"required"`` or
        ``{"type": "function", "function": {"name": "start_task"}}`` to force a call).
        """
        endpoints = self._endpoints()
        if not endpoints:
            raise LLMError("No LLM endpoint is configured. Set one in Settings.")

        last_error: Exception | None = None
        prepared = _normalise_messages_for_vision(messages)
        for client, model in endpoints:
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": prepared,
                "temperature": self.cfg.temperature if temperature is None else temperature,
                "stream": True,
            }
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = tool_choice or "auto"
            try:
                return await self._stream_once(client, kwargs, on_delta, cancel)
            except Interrupted:
                raise
            except APIConnectionError as exc:
                last_error = exc
                continue
            except APIError as exc:
                last_error = exc
                continue
            except (AttributeError, TypeError, KeyError, IndexError, LLMError) as exc:
                last_error = exc if isinstance(exc, LLMError) else LLMError(str(exc))
                continue
        raise LLMError(f"All configured LLM endpoints failed: {last_error}")

    async def _stream_once(
        self,
        client: AsyncOpenAI,
        kwargs: dict[str, Any],
        on_delta: DeltaFn | None,
        cancel: Any | None = None,
    ) -> ChatMessage:
        stream = await client.chat.completions.create(**kwargs)

        raw_content = ""
        reasoning_extra = ""
        tool_acc: dict[int, dict[str, str]] = {}
        prev_reasoning = ""
        prev_visible = ""
        stream_error: Exception | None = None

        try:
            async for chunk in stream:
                if cancel is not None and cancel.is_set():
                    raise Interrupted()
                if isinstance(chunk, str):
                    raise LLMError(
                        "LLM returned a string instead of a chat completion. "
                        "Point JARVIS_LLM_BASE_URL at an OpenAI-compatible /v1 endpoint "
                        "(e.g. http://localhost:11434/v1)."
                    )
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                delta = choices[0].delta

                rc = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
                if rc:
                    reasoning_extra += rc
                    if on_delta:
                        await on_delta({"kind": "reasoning", "text": rc})

                if delta.content:
                    raw_content += delta.content
                    if on_delta:
                        tagged, visible = split_thinking(raw_content)
                        if len(tagged) > len(prev_reasoning):
                            await on_delta({"kind": "reasoning", "text": tagged[len(prev_reasoning) :]})
                            prev_reasoning = tagged
                        if len(visible) > len(prev_visible):
                            await on_delta({"kind": "content", "text": visible[len(prev_visible) :]})
                            prev_visible = visible

                for tc in delta.tool_calls or []:
                    idx = tc.index if tc.index is not None else 0
                    slot = tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            slot["name"] = tc.function.name
                        if tc.function.arguments:
                            slot["arguments"] += tc.function.arguments
        except Interrupted:
            raise
        except Exception as exc:  # noqa: BLE001
            # Keep whatever tokens arrived so the agent can wrap up instead of
            # dying mid-thought with no chat reply (dropped SSE, local-server cutoffs).
            stream_error = exc
        finally:
            # Best-effort close so the upstream request stops when interrupted.
            close = getattr(stream, "close", None) or getattr(stream, "aclose", None)
            if close:
                try:
                    result = close()
                    if hasattr(result, "__await__"):
                        await result
                except Exception:  # noqa: BLE001
                    pass

        if stream_error and not (raw_content or reasoning_extra or tool_acc):
            raise LLMError(f"LLM stream ended: {stream_error}") from stream_error

        tagged_reasoning, visible = split_thinking(raw_content)
        reasoning = "\n\n".join(p for p in (reasoning_extra.strip(), tagged_reasoning) if p)

        tool_calls = []
        for idx in sorted(tool_acc):
            slot = tool_acc[idx]
            if not slot["name"] and not slot["arguments"]:
                continue
            # A dropped stream often leaves a half-written tool call; skip those so
            # the agent wraps up instead of executing garbage arguments.
            if stream_error and (not slot["name"] or not _looks_complete_json(slot["arguments"])):
                continue
            tool_calls.append(
                type(
                    "ToolCall",
                    (),
                    {
                        "id": slot["id"] or f"call_{idx}",
                        "function": type(
                            "Fn",
                            (),
                            {"name": slot["name"], "arguments": slot["arguments"]},
                        )(),
                    },
                )()
            )

        return ChatMessage(content=visible, reasoning=reasoning, tool_calls=tool_calls)

    async def embed(self, text: str) -> list[float] | None:
        """Return an embedding vector if an embedding endpoint is configured, else None."""
        base_url = self.cfg.embedding_base_url or self.cfg.base_url
        api_key = self.cfg.embedding_api_key or self.cfg.api_key or "local"
        model = self.cfg.embedding_model
        if not base_url or not model:
            return None
        client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        try:
            resp = await client.embeddings.create(model=model, input=text[:8000])
            return list(resp.data[0].embedding)
        except (APIConnectionError, APIError):
            return None
