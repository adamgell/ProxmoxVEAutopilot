"""Probe helpers for the operator onboarding wizard.

Each probe returns a dict shaped as:
    {"ok": bool, "detail": str, "checks": {<name>: {"ok": bool, "detail": str}, ...}}
"""
from __future__ import annotations

import socket
import subprocess


def _dns_resolve(domain: str) -> tuple[bool, str]:
    try:
        info = socket.getaddrinfo(domain, None)
        addr = info[0][4][0]
        return True, f"resolved {addr}"
    except socket.gaierror as e:
        return False, f"{type(e).__name__}: {e}"


def _icmp_ping(host: str) -> tuple[bool, str]:
    try:
        out = subprocess.run(
            ["ping", "-c", "1", "-W", "2", host],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0:
            return True, out.stdout.splitlines()[-1] if out.stdout else "ok"
        return False, "no reply (ICMP may be blocked; ignore if LDAP succeeds)"
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except FileNotFoundError:
        return False, "ping binary not found"
    except OSError as e:
        return False, f"ping failed: {e}"


def _ldap_bind(domain: str, account: str, password: str) -> tuple[bool, str]:
    """Use python-ldap with SASL/GSSAPI fallback to simple bind.

    The autopilot stack already wires python-ldap + libsasl2-modules-gssapi-mit
    (see autopilot-proxmox/web/app.py around line 3044 for the existing helper).
    Reuse that helper if present; otherwise fall back to a simple bind here.
    """
    try:
        import ldap  # type: ignore[import]
    except ImportError as e:
        return False, f"python-ldap import failed: {e}"
    try:
        conn = ldap.initialize(f"ldap://{domain}")
        conn.set_option(ldap.OPT_REFERRALS, 0)
        conn.set_option(ldap.OPT_NETWORK_TIMEOUT, 5)
    except ldap.LDAPError as e:
        return False, f"LDAPError: {e}"
    try:
        conn.simple_bind_s(f"{account}@{domain}", password)
        return True, f"bound as {account}@{domain}"
    except ldap.INVALID_CREDENTIALS:
        return False, "invalid credentials"
    except ldap.LDAPError as e:
        return False, f"LDAPError: {e}"
    finally:
        try:
            conn.unbind_s()
        except Exception:
            pass


def probe_ad(domain: str, account: str, password: str) -> dict:
    """Probe Active Directory reachability: DNS, ICMP, LDAP bind."""
    dns_ok, dns_detail = _dns_resolve(domain)
    icmp_ok, icmp_detail = _icmp_ping(domain) if dns_ok else (False, "not run (DNS failed)")
    ldap_ok, ldap_detail = (
        _ldap_bind(domain, account, password) if dns_ok else (False, "not run (DNS failed)")
    )
    # AD reachability decision: DNS + LDAP must succeed. ICMP is informational only.
    ok = dns_ok and ldap_ok
    if not dns_ok:
        detail = f"DNS does not resolve the domain: {dns_detail}"
    elif not ldap_ok:
        detail = f"LDAP bind refused: {ldap_detail}"
    else:
        detail = ldap_detail
    return {
        "ok": ok,
        "detail": detail,
        "checks": {
            "dns": {"ok": dns_ok, "detail": dns_detail},
            "icmp": {"ok": icmp_ok, "detail": icmp_detail},
            "ldap": {"ok": ldap_ok, "detail": ldap_detail},
        },
    }
