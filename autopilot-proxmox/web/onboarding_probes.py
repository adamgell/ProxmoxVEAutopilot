"""Probe helpers for the operator onboarding wizard.

Each probe returns a dict shaped as:
    {"ok": bool, "detail": str, "checks": {<name>: {"ok": bool, "detail": str}, ...}}
"""
from __future__ import annotations

import logging
import re
import socket
import subprocess
import urllib.request

_log = logging.getLogger("web.onboarding")


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
        # Homelab/AD DCs commonly present a self-signed cert, so ALLOW (do not
        # require a trusted CA). StartTLS below still encrypts the wire, which is
        # what protects the credential from passive sniffing.
        conn.set_option(ldap.OPT_X_TLS_REQUIRE_CERT, ldap.OPT_X_TLS_ALLOW)
        conn.set_option(ldap.OPT_X_TLS_NEWCTX, 0)
    except ldap.LDAPError as e:
        return False, f"LDAPError: {e}"
    tls = "starttls"
    try:
        conn.start_tls_s()
    except Exception as e:
        # StartTLS unavailable/failed: fall back to an unencrypted simple bind so
        # homelab AD without LDAPS still works, but make the plaintext explicit.
        tls = "cleartext"
        _log.warning(
            "LDAP StartTLS to %s failed (%s); falling back to an UNENCRYPTED simple "
            "bind - the AD credential is sent in cleartext.", domain, e,
        )
    try:
        conn.simple_bind_s(f"{account}@{domain}", password)
        return True, f"bound as {account}@{domain} ({tls})"
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


def _list_cloudosd_artifacts() -> list[dict]:
    """List CloudOSD artifacts via the same path the GET /artifacts endpoint uses.

    Deviation from the plan text: web.cloudosd_cache does NOT expose
    list_artifacts(); the cache module manages catalog downloads (entries), not
    built artifacts. cloudosd_pg.list_artifacts(conn) is the canonical source.
    We open the same _conn() the endpoint uses so connection setup + schema
    init mirror production.
    """
    from web import cloudosd_endpoints, cloudosd_pg  # type: ignore
    with cloudosd_endpoints._conn() as conn:
        return [
            cloudosd_endpoints.enrich_artifact(a) or a
            for a in cloudosd_pg.list_artifacts(conn)
        ]


def _list_osdeploy_artifacts() -> list[dict]:
    """List OSDeploy artifacts. Same deviation note as _list_cloudosd_artifacts."""
    from web import osdeploy_endpoints, osdeploy_pg  # type: ignore
    with osdeploy_endpoints._conn() as conn:
        return [
            osdeploy_endpoints.enrich_artifact(a) or a
            for a in osdeploy_pg.list_artifacts(conn)
        ]


def probe_artifact() -> dict:
    """Inventory CloudOSD + OSDeploy artifact stores.

    ok is True iff at least one artifact exists somewhere. Each kind is fetched
    independently: a failure in one (e.g. cloudosd_pg schema not initialised)
    leaves the other intact. The broad except is intentional because deployment
    shapes vary -- the DB might be unconfigured, the schema not yet migrated, or
    one of the import paths may not be loaded.
    """
    try:
        cloudosd = list(_list_cloudosd_artifacts())
    except Exception:
        cloudosd = []
    try:
        osdeploy = list(_list_osdeploy_artifacts())
    except Exception:
        osdeploy = []
    return {
        "ok": bool(cloudosd or osdeploy),
        "detail": f"{len(cloudosd)} CloudOSD, {len(osdeploy)} OSDeploy",
        "cloudosd": cloudosd,
        "osdeploy": osdeploy,
    }


_TENANT_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z", re.IGNORECASE)


def probe_tenant(tenant_id: str, tenant_domain: str, *, graph_check: bool = True) -> dict:
    """Validate Autopilot tenant values. Shape check first; optional Graph sanity-check."""
    if not _TENANT_ID_RE.match(tenant_id or ""):
        return {
            "ok": False,
            "detail": "Tenant id format invalid. Check the value in https://entra.microsoft.com under Overview.",
            "checks": {"shape": {"ok": False, "detail": "not a uuid"}},
        }
    if not tenant_domain or "." not in tenant_domain:
        return {
            "ok": False,
            "detail": "Tenant domain looks wrong; expected something like contoso.onmicrosoft.com.",
            "checks": {"shape": {"ok": True}, "domain": {"ok": False, "detail": "missing dot"}},
        }
    if not graph_check:
        return {
            "ok": True,
            "detail": "shape ok; Graph sanity-check skipped",
            "checks": {"shape": {"ok": True}, "domain": {"ok": True}},
        }
    # Graph sanity check: hit the OpenID metadata endpoint, no creds needed.
    try:
        with urllib.request.urlopen(
            f"https://login.microsoftonline.com/{tenant_id}/v2.0/.well-known/openid-configuration",
            timeout=5,
        ) as r:
            if r.status == 200:
                return {
                    "ok": True,
                    "detail": "tenant resolves on login.microsoftonline.com",
                    "checks": {"shape": {"ok": True}, "domain": {"ok": True}, "graph": {"ok": True}},
                }
            return {
                "ok": False,
                "detail": f"login.microsoftonline.com returned {r.status}",
                "checks": {"shape": {"ok": True}, "domain": {"ok": True}, "graph": {"ok": False}},
            }
    except Exception as e:
        return {
            "ok": False,
            "detail": f"could not reach login.microsoftonline.com: {e}",
            "checks": {"shape": {"ok": True}, "domain": {"ok": True}, "graph": {"ok": False}},
        }
