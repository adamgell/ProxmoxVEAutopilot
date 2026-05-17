from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable
from uuid import UUID

from fastapi import HTTPException


JsonDict = dict[str, Any]
ToolHandler = Callable[[JsonDict], Any]
SECRET_KEY_RE = re.compile(r"(password|secret|token|authorization|api[_-]?key|private[_-]?key)", re.I)
SECRET_VALUE_RE = re.compile(r"(?i)(bearer\s+|token=|password=|secret=)([^\\s&]+)")


def jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    return value


def redact(value: Any, *, max_string: int = 12000) -> Any:
    value = jsonable(value)
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if SECRET_KEY_RE.search(str(key)):
                redacted[str(key)] = "[redacted]"
            else:
                redacted[str(key)] = redact(item, max_string=max_string)
        return redacted
    if isinstance(value, list):
        return [redact(item, max_string=max_string) for item in value]
    if isinstance(value, str):
        text = SECRET_VALUE_RE.sub(lambda match: f"{match.group(1)}[redacted]", value)
        if len(text) > max_string:
            return text[:max_string] + "...[truncated]"
        return text
    return value


def object_schema(properties: JsonDict | None = None, required: list[str] | None = None) -> JsonDict:
    schema: JsonDict = {
        "type": "object",
        "properties": properties or {},
        "additionalProperties": True,
    }
    if required:
        schema["required"] = required
    return schema


def any_output_schema() -> JsonDict:
    return object_schema()


def normalize_exception(exc: Exception) -> JsonDict:
    if isinstance(exc, HTTPException):
        return {
            "error": True,
            "status_code": exc.status_code,
            "detail": redact(exc.detail),
        }
    return {"error": True, "detail": redact(str(exc)), "type": exc.__class__.__name__}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: JsonDict
    handler: ToolHandler
    output_schema: JsonDict = field(default_factory=any_output_schema)
    annotations: JsonDict = field(default_factory=dict)

    def spec(self) -> JsonDict:
        spec: JsonDict = {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "outputSchema": self.output_schema,
        }
        if self.annotations:
            spec["annotations"] = self.annotations
        return spec


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(
        self,
        name: str,
        description: str,
        input_schema: JsonDict | None = None,
        output_schema: JsonDict | None = None,
        annotations: JsonDict | None = None,
    ) -> Callable[[ToolHandler], ToolHandler]:
        def decorator(handler: ToolHandler) -> ToolHandler:
            self._tools[name] = Tool(
                name=name,
                description=description,
                input_schema=input_schema or object_schema(),
                output_schema=output_schema or any_output_schema(),
                annotations=annotations or {},
                handler=handler,
            )
            return handler

        return decorator

    def specs(self) -> list[JsonDict]:
        return [self._tools[name].spec() for name in sorted(self._tools)]

    async def call(self, name: str, arguments: JsonDict | None = None) -> JsonDict:
        tool = self._tools.get(name)
        if not tool:
            raise KeyError(name)
        args = arguments or {}
        result = tool.handler(args)
        if asyncio.iscoroutine(result):
            result = await result
        return jsonable(result)

    def names(self) -> list[str]:
        return sorted(self._tools)


def tool_text_result(payload: Any, *, is_error: bool = False) -> JsonDict:
    structured = redact(payload)
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(structured, sort_keys=True, default=str),
            }
        ],
        "structuredContent": structured,
        "isError": is_error,
    }
