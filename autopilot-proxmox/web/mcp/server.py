from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from . import mcp_pg
from .registry import ToolRegistry, normalize_exception, tool_text_result


PROTOCOL_VERSION = "2025-06-18"


def _build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    from . import tools_agent, tools_cloudosd, tools_docs, tools_osdeploy, tools_pve, tools_setup, tools_ubuntu

    for module in (tools_docs, tools_pve, tools_cloudosd, tools_agent, tools_setup, tools_osdeploy, tools_ubuntu):
        module.register(registry)
    return registry


app = FastAPI(title="ProxmoxVEAutopilot MCP")
registry = _build_registry()


def _allowed_origins() -> set[str]:
    raw = os.environ.get(
        "AUTOPILOT_MCP_ALLOWED_ORIGINS",
        "http://localhost,http://127.0.0.1,http://localhost:15051,http://127.0.0.1:15051",
    )
    return {item.strip() for item in raw.split(",") if item.strip()}


def _check_auth(authorization: str | None, origin: str | None) -> None:
    expected = os.environ.get("AUTOPILOT_MCP_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="AUTOPILOT_MCP_TOKEN is required")
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="invalid MCP bearer token")
    if origin and origin not in _allowed_origins():
        raise HTTPException(status_code=403, detail="MCP origin is not allowed")


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"ok": True, "tools": len(registry.names())}


@app.post("/mcp", response_model=None)
async def mcp(
    request: Request,
    authorization: str | None = Header(default=None),
    origin: str | None = Header(default=None),
):
    _check_auth(authorization, origin)
    try:
        payload = await request.json()
    except Exception as exc:
        mcp_pg.audit_call(tool_name=None, arguments={}, error=f"invalid json: {exc}")
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="MCP request must be a JSON object")
    request_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    if request_id is None and str(method).startswith("notifications/"):
        return PlainTextResponse("", status_code=202)
    try:
        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "ProxmoxVEAutopilot", "version": "1.0"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": registry.specs()}
        elif method == "tools/call":
            name = str(params.get("name") or "")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise ValueError("tools/call arguments must be an object")
            structured = await registry.call(name, arguments)
            result = tool_text_result(structured)
            mcp_pg.audit_call(tool_name=name, arguments=arguments, result=structured)
        else:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"unknown method: {method}"},
                }
            )
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})
    except KeyError as exc:
        name = str(exc)
        mcp_pg.audit_call(tool_name=name, arguments=params, error="unknown tool")
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": f"unknown tool: {name}"},
            }
        )
    except Exception as exc:
        tool_name = str(params.get("name") or "") if isinstance(params, dict) else None
        error_payload = normalize_exception(exc)
        mcp_pg.audit_call(tool_name=tool_name, arguments=params, result=error_payload, error=str(exc))
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": tool_text_result(error_payload, is_error=True),
            }
        )


@app.on_event("startup")
def _startup() -> None:
    mcp_pg.init()
