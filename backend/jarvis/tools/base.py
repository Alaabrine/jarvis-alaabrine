"""Base types for JARVIS tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..config import Config


@dataclass
class ToolContext:
    """Ambient dependencies passed to every tool invocation."""

    config: Config
    memory: Any  # jarvis.memory.Memory (avoid circular import)
    llm: Any     # jarvis.llm.LLMClient
    tasks: Any = None  # jarvis.tasks.TaskManager (avoid circular import)
    depth: int = 0     # 0 = main agent, 1 = background subagent
    conversation_id: int | None = None  # origin conversation of the current run


@dataclass
class ToolResult:
    ok: bool
    output: str


DangerFn = Callable[[dict[str, Any]], bool]
RunFn = Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    run: RunFn
    # Whether this call needs user confirmation. Either a bool or a predicate over args.
    dangerous: bool | DangerFn = False
    # Optional human-readable preview of what will happen, for the confirmation dialog.
    preview: Callable[[dict[str, Any]], str] | None = None

    def is_dangerous(self, args: dict[str, Any]) -> bool:
        if callable(self.dangerous):
            return bool(self.dangerous(args))
        return bool(self.dangerous)

    def make_preview(self, args: dict[str, Any]) -> str:
        if self.preview:
            return self.preview(args)
        return f"{self.name}({args})"

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": clean_schema(self.parameters),
                    "required": [
                        k for k, v in self.parameters.items() if not v.get("_optional")
                    ],
                },
            },
        }


def prop(type_: str, description: str, optional: bool = False, **extra: Any) -> dict[str, Any]:
    """Helper to build a JSON-schema property (with an internal _optional marker)."""
    d: dict[str, Any] = {"type": type_, "description": description}
    d.update(extra)
    if optional:
        d["_optional"] = True
    return d


def clean_schema(parameters: dict[str, Any]) -> dict[str, Any]:
    """Strip internal markers before sending schema to the model."""
    cleaned = {}
    for name, spec in parameters.items():
        cleaned[name] = {k: v for k, v in spec.items() if k != "_optional"}
    return cleaned
