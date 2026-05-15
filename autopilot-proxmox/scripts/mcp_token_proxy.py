#!/usr/bin/env python3
"""Local MCP token-injecting HTTP proxy.

This lets GUI clients point at a stable unauthenticated localhost endpoint while
the proxy adds the private MCP bearer token when forwarding to the real MCP
server through an SSH tunnel.
"""

from __future__ import annotations

import http.client
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


LISTEN_HOST = os.environ.get("AUTOPILOT_MCP_PROXY_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("AUTOPILOT_MCP_PROXY_PORT", "15051"))
TARGET_BASE = os.environ.get("AUTOPILOT_MCP_PROXY_TARGET", "http://127.0.0.1:15050")
TOKEN = os.environ.get("AUTOPILOT_MCP_TOKEN", "")
ORIGIN = os.environ.get("AUTOPILOT_MCP_ORIGIN", "http://localhost")


class ProxyHandler(BaseHTTPRequestHandler):
    server_version = "AutopilotMcpTokenProxy/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), fmt % args))

    def do_GET(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def do_DELETE(self) -> None:
        self._forward()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", ORIGIN))
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, Mcp-Session-Id")
        self.end_headers()

    def _forward(self) -> None:
        if not TOKEN:
            self._send_plain(503, b"AUTOPILOT_MCP_TOKEN is not configured\n")
            return

        target = urllib.parse.urljoin(TARGET_BASE.rstrip("/") + "/", self.path.lstrip("/"))
        parsed = urllib.parse.urlparse(target)
        body = self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))

        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower()
            not in {
                "host",
                "authorization",
                "content-length",
                "connection",
                "proxy-connection",
            }
        }
        headers["Authorization"] = f"Bearer {TOKEN}"
        headers["Origin"] = headers.get("Origin") or ORIGIN
        if body:
            headers["Content-Length"] = str(len(body))

        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        try:
            conn = conn_cls(parsed.hostname, parsed.port, timeout=65)
            conn.request(self.command, path, body=body, headers=headers)
            response = conn.getresponse()
            data = response.read()
        except OSError as exc:
            self._send_plain(502, f"MCP backend unavailable: {exc}\n".encode("utf-8", "replace"))
            return
        finally:
            try:
                conn.close()  # type: ignore[possibly-undefined]
            except Exception:
                pass

        self.send_response(response.status, response.reason)
        excluded = {"connection", "transfer-encoding", "content-length"}
        for key, value in response.getheaders():
            if key.lower() not in excluded:
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_plain(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    if not TOKEN:
        print("warning: AUTOPILOT_MCP_TOKEN is empty; proxy will return 503", file=sys.stderr)
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)
    print(
        f"listening on http://{LISTEN_HOST}:{LISTEN_PORT}/mcp -> {TARGET_BASE.rstrip('/')}/mcp",
        file=sys.stderr,
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
