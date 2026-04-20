"""Test an AD `domain_join` credential via ldap3.

DNS SRV lookup → TLS connect → bind → rootDSE read → optional OU
search. Returns a stage-by-stage dict so the UI can render green/red
checklist rows. Never echoes the submitted password in any stage's
response (including error text).
"""
from __future__ import annotations

import socket
import time
from typing import Optional

from ldap3 import Connection, Server, SUBTREE, Tls
import ssl


# Per-stage timeout (seconds); total budget capped by the sum of stages.
_STAGE_TIMEOUT = 8
_TOTAL_BUDGET = 30


def test_domain_join(payload: dict, *, validate_certs: bool) -> dict:
    """Run the five-stage test. ``payload`` is a ``domain_join``-typed
    credential dict (domain_fqdn, username, password, optional ou_hint).
    """
    out: dict = {
        "ok": False,
        "dns": {"ok": False},
        "connect": {"ok": False},
        "bind": {"ok": False},
        "rootdse": {"ok": False},
        "ou": {"ok": True},  # default ok when ou_hint is absent
    }
    fqdn = (payload.get("domain_fqdn") or "").strip()
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    ou_hint = (payload.get("ou_hint") or "").strip()

    # 1. DNS SRV
    try:
        servers, dns_ms = _dns_srv_lookup(fqdn)
    except Exception as e:
        out["dns"] = {"ok": False, "error": str(e)}
        return out
    out["dns"] = {"ok": True, "servers": servers, "elapsed_ms": dns_ms}

    # 2-4. connect + bind + rootDSE (attempt servers in order)
    try:
        connect_info, bind_info, rootdse_info = _try_bind(
            servers, username, password, validate_certs=validate_certs,
        )
    except Exception as e:
        out["connect"] = {"ok": False, "error": str(e)}
        return out
    out["connect"] = connect_info
    out["bind"] = bind_info
    if not bind_info["ok"]:
        return out
    if rootdse_info:
        out["rootdse"] = rootdse_info

    # 5. Optional OU search
    if ou_hint:
        try:
            ou_info = _search_ou(servers[0], username, password, ou_hint,
                                  validate_certs=validate_certs)
        except Exception as e:
            out["ou"] = {"ok": False, "error": str(e)}
            return out
        out["ou"] = ou_info
        if not ou_info["ok"]:
            return out

    out["ok"] = True
    return out


def _dns_srv_lookup(fqdn: str) -> tuple[list[str], int]:
    """Resolve _ldap._tcp.<fqdn> to an ordered list of DC hostnames."""
    import dns.resolver  # dnspython — transitive of ldap3 in recent versions
    start = time.monotonic()
    answers = dns.resolver.resolve(f"_ldap._tcp.{fqdn}", "SRV",
                                    lifetime=_STAGE_TIMEOUT)
    # SRV records are (priority, weight, port, target). Sort by priority.
    ordered = sorted(answers, key=lambda a: (a.priority, -a.weight))
    hosts = [str(a.target).rstrip(".") for a in ordered]
    ms = int((time.monotonic() - start) * 1000)
    return hosts, ms


def _try_bind(servers: list[str], username: str, password: str, *,
              validate_certs: bool) -> tuple[dict, dict, Optional[dict]]:
    """Attempt LDAPS (636) then LDAP+StartTLS (389) against the first
    responsive server. Returns (connect_info, bind_info, rootdse_info).
    """
    last_error = None
    for host in servers:
        for port, tls_mode in ((636, "ldaps"), (389, "starttls")):
            try:
                start = time.monotonic()
                tls = Tls(
                    validate=ssl.CERT_REQUIRED if validate_certs else ssl.CERT_NONE,
                    version=ssl.PROTOCOL_TLS_CLIENT,
                )
                server = Server(host, port=port, use_ssl=(tls_mode == "ldaps"),
                                tls=tls, connect_timeout=_STAGE_TIMEOUT,
                                get_info="ALL")
                connect_ms = int((time.monotonic() - start) * 1000)
                connect_info = {"ok": True, "server": host, "tls": tls_mode,
                                "elapsed_ms": connect_ms}

                bind_start = time.monotonic()
                conn = Connection(server, user=username, password=password,
                                  auto_bind="TLS_BEFORE_BIND"
                                  if tls_mode == "starttls" else "DEFAULT",
                                  receive_timeout=_STAGE_TIMEOUT)
                bind_ms = int((time.monotonic() - bind_start) * 1000)
                bind_info = {"ok": True, "elapsed_ms": bind_ms}

                # rootDSE from server.info (ldap3 populates on bind)
                info = server.info
                rootdse_info = {
                    "ok": True,
                    "defaultNamingContext":
                        str(info.other.get("defaultNamingContext", [""])[0])
                        if info else "",
                    "dnsHostName":
                        str(info.other.get("dnsHostName", [""])[0])
                        if info else "",
                }
                conn.unbind()
                return connect_info, bind_info, rootdse_info
            except Exception as e:
                msg = str(e)
                # Strip any embedded password echoes just in case the ldap3
                # traceback ever includes them.
                if password and password in msg:
                    msg = msg.replace(password, "***")
                last_error = msg
                # Try next port/host
                continue
    # All attempts failed — report the last one's error, but on the bind
    # stage (not connect) since reaching here usually means bind failed.
    return (
        {"ok": False, "error": last_error or "all servers unreachable"},
        {"ok": False, "error": last_error or "bind failed", "elapsed_ms": 0},
        None,
    )


def _search_ou(host: str, username: str, password: str, ou_dn: str, *,
               validate_certs: bool) -> dict:
    """Search for the OU DN to confirm it exists and is visible to the
    bind account. Uses LDAPS only for simplicity."""
    start = time.monotonic()
    tls = Tls(
        validate=ssl.CERT_REQUIRED if validate_certs else ssl.CERT_NONE,
        version=ssl.PROTOCOL_TLS_CLIENT,
    )
    server = Server(host, port=636, use_ssl=True, tls=tls,
                    connect_timeout=_STAGE_TIMEOUT)
    conn = Connection(server, user=username, password=password,
                      auto_bind="DEFAULT", receive_timeout=_STAGE_TIMEOUT)
    try:
        # base-level search for the exact OU
        found = conn.search(search_base=ou_dn, search_filter="(objectClass=*)",
                            search_scope="BASE")
        ms = int((time.monotonic() - start) * 1000)
        if found and conn.entries:
            return {"ok": True, "dn": ou_dn, "elapsed_ms": ms}
        return {"ok": False, "error": f"noSuchObject: {ou_dn}",
                "elapsed_ms": ms}
    finally:
        conn.unbind()
