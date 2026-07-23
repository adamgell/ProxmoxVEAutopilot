"""Proxmox VE HTTP client helpers extracted from web.app.

Depends on web.config_store for ``_load_proxmox_config`` (which is why
config_store must be extracted first) plus stdlib + requests. It must never
import ``web.app``. ``web.app`` re-exports every public name here so existing
``web_app._proxmox_api`` call sites and the heavy
``monkeypatch.setattr(web.app, "_proxmox_api", ...)`` test usage keep resolving
through the web.app namespace.

Two things the re-export shim does not cover, both verified safe against the
current suite but worth remembering:
  * Intra-module sibling calls (the HTTP helpers -> ``_load_proxmox_config``)
    resolve inside this module, bypassing an app-namespace monkeypatch. No test
    drives those chains with a partial patch. NB: ``_proxmox_node_ssh_host`` is
    deliberately NOT here - it calls ``_proxmox_api`` as a sibling and is
    exercised by tests that patch ``web.app._proxmox_api``, so it stays
    app-resident to resolve that patch through app's namespace.
  * ``requests`` is imported here as the same module object app.py imports, so
    ``patch("web.app.requests.post")`` still intercepts calls made from here.
"""
from __future__ import annotations

import io
import secrets
from pathlib import Path

import requests
import urllib3

from web.config_store import _load_proxmox_config

# verify=False is used throughout (self-signed Proxmox certs); silence the
# per-call warning here too since app.py is not always the import entrypoint.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _proxmox_api(path, method="GET", data=None, files=None):
    cfg = _load_proxmox_config()
    host = cfg.get("proxmox_host", "")
    port = cfg.get("proxmox_port", 8006)
    token_id = cfg.get("vault_proxmox_api_token_id", "")
    token_secret = cfg.get("vault_proxmox_api_token_secret", "")
    url = f"https://{host}:{port}/api2/json{path}"
    headers = {"Authorization": f"PVEAPIToken={token_id}={token_secret}"}
    resp = requests.request(
        method, url, headers=headers, data=data, files=files,
        verify=False,
        timeout=30 if files else 10,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


class _StreamingMultipartBody:
    _DEFAULT_READ_SIZE = 1024 * 1024

    def __init__(
        self,
        *,
        fields: dict[str, str],
        file_field: str,
        file_path: Path,
        content_type: str,
    ):
        self.boundary = f"----ProxmoxVEAutopilot{secrets.token_hex(16)}"
        field_parts: list[bytes] = []
        for name, value in fields.items():
            field_parts.append(
                (
                    f"--{self.boundary}\r\n"
                    f"Content-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                    f"{value}\r\n"
                ).encode("utf-8")
            )
        field_parts.append(
            (
                f"--{self.boundary}\r\n"
                f"Content-Disposition: form-data; name=\"{file_field}\"; "
                f"filename=\"{file_path.name}\"\r\n"
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
        )
        self._prefix = io.BytesIO(b"".join(field_parts))
        self._file = file_path.open("rb")
        self._suffix = io.BytesIO(f"\r\n--{self.boundary}--\r\n".encode("utf-8"))
        self._segments = [self._prefix, self._file, self._suffix]
        self._index = 0
        self.length = sum(len(part) for part in field_parts) + file_path.stat().st_size + len(self._suffix.getvalue())
        self.content_type = f"multipart/form-data; boundary={self.boundary}"

    def __len__(self) -> int:
        return self.length

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = self._DEFAULT_READ_SIZE
        chunks: list[bytes] = []
        remaining = size
        while self._index < len(self._segments) and remaining != 0:
            segment = self._segments[self._index]
            chunk = segment.read(remaining)
            if chunk:
                chunks.append(chunk)
                remaining -= len(chunk)
                if remaining <= 0:
                    break
            else:
                self._index += 1
        return b"".join(chunks)

    def close(self) -> None:
        self._file.close()


def _proxmox_upload_file(
    path: str,
    file_path: Path,
    *,
    data: dict[str, str] | None = None,
    field_name: str = "filename",
    content_type: str = "application/octet-stream",
):
    cfg = _load_proxmox_config()
    host = cfg.get("proxmox_host", "")
    port = cfg.get("proxmox_port", 8006)
    token_id = cfg.get("vault_proxmox_api_token_id", "")
    token_secret = cfg.get("vault_proxmox_api_token_secret", "")
    url = f"https://{host}:{port}/api2/json{path}"
    body = _StreamingMultipartBody(
        fields={key: str(value) for key, value in (data or {}).items()},
        file_field=field_name,
        file_path=file_path,
        content_type=content_type,
    )
    headers = {
        "Authorization": f"PVEAPIToken={token_id}={token_secret}",
        "Content-Type": body.content_type,
        "Content-Length": str(len(body)),
    }
    try:
        resp = requests.post(url, headers=headers, data=body, verify=False, timeout=600)
        resp.raise_for_status()
        return resp.json().get("data", [])
    finally:
        body.close()


def _proxmox_api_post(path, data=None):
    """POST to Proxmox API (for VM power actions and guest-exec)."""
    cfg = _load_proxmox_config()
    host = cfg.get("proxmox_host", "")
    port = cfg.get("proxmox_port", 8006)
    token_id = cfg.get("vault_proxmox_api_token_id", "")
    token_secret = cfg.get("vault_proxmox_api_token_secret", "")
    url = f"https://{host}:{port}/api2/json{path}"
    headers = {"Authorization": f"PVEAPIToken={token_id}={token_secret}"}
    resp = requests.post(url, headers=headers, data=data, verify=False, timeout=10)
    resp.raise_for_status()
    return resp.json().get("data", {})


def _proxmox_api_put(path, data=None):
    """PUT to Proxmox API (for VM config changes)."""
    cfg = _load_proxmox_config()
    host = cfg.get("proxmox_host", "")
    port = cfg.get("proxmox_port", 8006)
    token_id = cfg.get("vault_proxmox_api_token_id", "")
    token_secret = cfg.get("vault_proxmox_api_token_secret", "")
    url = f"https://{host}:{port}/api2/json{path}"
    headers = {"Authorization": f"PVEAPIToken={token_id}={token_secret}"}
    resp = requests.put(url, headers=headers, data=data, verify=False, timeout=10)
    resp.raise_for_status()
    return resp.json().get("data", {})


def _proxmox_root_ticket_fetch(cfg: dict) -> tuple[str, str]:
    """Exchange root@pam username/password for a (ticket, CSRF) pair.

    Proxmox tickets are good for ~2 hours by default. Newer runtime
    paths prefer root SSH for host-local QEMU args work, but this helper
    remains for compatibility with older call sites and tests.
    """
    host = cfg.get("proxmox_host", "")
    port = cfg.get("proxmox_port", 8006)
    username = (cfg.get("vault_proxmox_root_username") or "root@pam").strip()
    # Proxmox /access/ticket demands <user>@<realm>. A bare 'root' gets
    # a 401 that looks like a wrong-password error; tolerate it by
    # defaulting to the @pam realm.
    if "@" not in username:
        username = f"{username}@pam"
    password = cfg.get("vault_proxmox_root_password") or ""
    if not password:
        raise ValueError("vault_proxmox_root_password is empty")
    url = f"https://{host}:{port}/api2/json/access/ticket"
    resp = requests.post(
        url,
        data={"username": username, "password": password},
        verify=cfg.get("proxmox_validate_certs", False),
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json().get("data") or {}
    ticket = data.get("ticket")
    csrf = data.get("CSRFPreventionToken")
    if not (ticket and csrf):
        raise RuntimeError(
            f"/access/ticket response missing ticket or CSRF token: {data!r}"
        )
    return ticket, csrf


def _proxmox_api_delete(path):
    """DELETE to Proxmox API (for VM removal)."""
    cfg = _load_proxmox_config()
    host = cfg.get("proxmox_host", "")
    port = cfg.get("proxmox_port", 8006)
    token_id = cfg.get("vault_proxmox_api_token_id", "")
    token_secret = cfg.get("vault_proxmox_api_token_secret", "")
    url = f"https://{host}:{port}/api2/json{path}"
    headers = {"Authorization": f"PVEAPIToken={token_id}={token_secret}"}
    resp = requests.delete(url, headers=headers, verify=False, timeout=10)
    resp.raise_for_status()
    return resp.json().get("data", {})
