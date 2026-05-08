"""Realtime WebSocket hub for operator UI state.

The hub owns one collector task per web process and fans results out to every
connected browser. Individual tabs update subscriptions, but they do not start
their own Proxmox/QGA polling loops.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from fastapi import WebSocket, WebSocketDisconnect


SnapshotProvider = Callable[[set[str], set[int]], Awaitable[list[dict[str, Any]]]]
PatchProvider = Callable[[set[str], set[int], bool], Awaitable[list[dict[str, Any]]]]
RefreshHandler = Callable[[str], Awaitable[list[dict[str, Any]]]]
QgaProbeHandler = Callable[[int], Awaitable[dict[str, Any]]]
ScreenshotHandler = Callable[[int, str], Awaitable[dict[str, Any]]]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(eq=False)
class LiveClient:
    websocket: WebSocket
    topics: set[str] = field(default_factory=set)
    vmids: set[int] = field(default_factory=set)

    async def send(self, message: dict[str, Any]) -> None:
        await self.websocket.send_text(json.dumps(message, default=str))


class LiveHub:
    def __init__(
        self,
        *,
        snapshot_provider: SnapshotProvider,
        patch_provider: PatchProvider,
        refresh_handler: RefreshHandler,
        qga_probe_handler: QgaProbeHandler,
        screenshot_handler: ScreenshotHandler,
        poll_interval_seconds: float = 2.0,
        qga_interval_seconds: float | None = None,
    ):
        self.snapshot_provider = snapshot_provider
        self.patch_provider = patch_provider
        self.refresh_handler = refresh_handler
        self.qga_probe_handler = qga_probe_handler
        self.screenshot_handler = screenshot_handler
        self.poll_interval_seconds = poll_interval_seconds
        self.qga_interval_seconds = qga_interval_seconds
        self.clients: set[LiveClient] = set()
        self.collector_task: asyncio.Task | None = None
        self.version = 0
        self.collector_starts = 0
        self._last_qga_at = 0.0
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        client = LiveClient(websocket=websocket)
        self.clients.add(client)
        await client.send({
            "type": "hello",
            "schema_version": 1,
            "server_time": utc_now_iso(),
        })
        await self._ensure_collector()
        try:
            while True:
                raw = await websocket.receive_text()
                await self._handle_message(client, raw)
        except WebSocketDisconnect:
            pass
        finally:
            self.clients.discard(client)

    async def broadcast(self, message: dict[str, Any]) -> None:
        topic = message.get("topic")
        dead: list[LiveClient] = []
        for client in list(self.clients):
            if topic and client.topics and topic not in client.topics:
                continue
            try:
                await client.send(message)
            except Exception:
                dead.append(client)
        for client in dead:
            self.clients.discard(client)

    async def _ensure_collector(self) -> None:
        async with self._lock:
            if self.collector_task is None or self.collector_task.done():
                self.collector_starts += 1
                self.collector_task = asyncio.create_task(self._collector_loop())

    async def _handle_message(self, client: LiveClient, raw: str) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            await client.send({
                "type": "error",
                "error": "invalid_json",
                "detail": "message is not valid JSON",
            })
            return

        msg_type = message.get("type")
        correlation_id = message.get("correlation_id")
        if msg_type == "subscribe":
            topics = message.get("topics") or []
            client.topics = {str(t) for t in topics if t}
            vmids = message.get("vmids") or []
            client.vmids = {int(v) for v in vmids if str(v).isdigit()}
            snapshots = await self.snapshot_provider(client.topics, client.vmids)
            for snapshot in snapshots:
                self.version += 1
                snapshot.setdefault("version", self.version)
                await client.send(snapshot)
            return

        if msg_type == "refresh":
            scope = str(message.get("scope") or "fleet")
            start_event = {
                "type": "event",
                "topic": "fleet" if scope == "fleet" else scope,
                "event": "sweep_started" if scope == "fleet" else "refresh_started",
                "scope": scope,
                "generated_at": utc_now_iso(),
            }
            self.version += 1
            start_event["version"] = self.version
            await self.broadcast(start_event)
            try:
                events = await self.refresh_handler(scope)
            except Exception as exc:
                await client.send({
                    "type": "error",
                    "correlation_id": correlation_id,
                    "error": "refresh_failed",
                    "detail": str(exc),
                })
                return
            for event in events:
                self.version += 1
                event.setdefault("version", self.version)
                await self.broadcast(event)
            return

        if msg_type == "qga_probe":
            try:
                vmid = int(message.get("vmid"))
                result = await self.qga_probe_handler(vmid)
                await client.send({
                    "type": "event",
                    "topic": "fleet",
                    "event": "qga_probe.result",
                    "correlation_id": correlation_id,
                    "vmid": vmid,
                    "result": result,
                    "generated_at": utc_now_iso(),
                })
            except Exception as exc:
                await client.send({
                    "type": "error",
                    "topic": "fleet",
                    "correlation_id": correlation_id,
                    "error": "qga_probe_failed",
                    "detail": str(exc),
                })
            return

        if msg_type == "screenshot.request":
            try:
                vmid = int(message.get("vmid"))
                fmt = str(message.get("format") or "png")
                result = await self.screenshot_handler(vmid, fmt)
                await client.send({
                    "type": "screenshot.result",
                    "topic": "fleet",
                    "correlation_id": correlation_id,
                    **result,
                })
            except Exception as exc:
                await client.send({
                    "type": "error",
                    "topic": "fleet",
                    "correlation_id": correlation_id,
                    "error": "screenshot_failed",
                    "detail": str(exc),
                })
            return

        await client.send({
            "type": "error",
            "correlation_id": correlation_id,
            "error": "unknown_message_type",
            "detail": str(msg_type),
        })

    async def _collector_loop(self) -> None:
        while self.clients:
            await asyncio.sleep(self.poll_interval_seconds)
            if not self.clients:
                break
            topics = set().union(*(c.topics for c in self.clients)) if self.clients else set()
            vmids = set().union(*(c.vmids for c in self.clients)) if self.clients else set()
            include_qga = (
                self.qga_interval_seconds is not None
                and (time.monotonic() - self._last_qga_at) >= self.qga_interval_seconds
            )
            if include_qga:
                self._last_qga_at = time.monotonic()
            if topics:
                try:
                    messages = await self.patch_provider(topics, vmids, include_qga)
                    for message in messages:
                        self.version += 1
                        message.setdefault("version", self.version)
                        await self.broadcast(message)
                except Exception as exc:
                    await self.broadcast({
                        "type": "error",
                        "error": "collector_failed",
                        "detail": str(exc),
                        "generated_at": utc_now_iso(),
                    })
            self.version += 1
            await self.broadcast({
                "type": "heartbeat",
                "version": self.version,
                "server_time": utc_now_iso(),
            })
