"""Tests for web/onboarding_probes.py."""
from __future__ import annotations

import pytest

from web import onboarding_probes


def test_probe_ad_returns_per_check_results(monkeypatch):
    def fake_dns(domain):
        return True, "resolved 192.168.2.10"
    def fake_icmp(host):
        return True, "1 round-trip 2ms"
    def fake_ldap_bind(domain, account, password):
        return True, "bound as svc-autopilot"
    monkeypatch.setattr(onboarding_probes, "_dns_resolve", fake_dns)
    monkeypatch.setattr(onboarding_probes, "_icmp_ping", fake_icmp)
    monkeypatch.setattr(onboarding_probes, "_ldap_bind", fake_ldap_bind)

    result = onboarding_probes.probe_ad("home.gell.one", "svc-autopilot", "pw")
    assert result["ok"] is True
    assert result["checks"]["dns"]["ok"] is True
    assert result["checks"]["icmp"]["ok"] is True
    assert result["checks"]["ldap"]["ok"] is True


def test_probe_ad_reports_first_failing_check_in_detail(monkeypatch):
    monkeypatch.setattr(onboarding_probes, "_dns_resolve", lambda d: (False, "NXDOMAIN"))
    monkeypatch.setattr(onboarding_probes, "_icmp_ping", lambda h: (False, "not run"))
    monkeypatch.setattr(onboarding_probes, "_ldap_bind", lambda *a: (False, "not run"))
    result = onboarding_probes.probe_ad("nope.example.com", "x", "x")
    assert result["ok"] is False
    assert "NXDOMAIN" in result["detail"]


def test_probe_ad_handles_missing_ping_binary(monkeypatch):
    """ICMP failure must not crash the probe."""
    monkeypatch.setattr(onboarding_probes, "_dns_resolve", lambda d: (True, "resolved"))

    import subprocess as _sp
    def fake_subprocess_run(*a, **kw):
        raise FileNotFoundError("ping not on PATH")
    monkeypatch.setattr(_sp, "run", fake_subprocess_run)
    ok, detail = onboarding_probes._icmp_ping("home.gell.one")
    assert ok is False
    assert "ping binary not found" in detail


def test_ldap_bind_unbinds_on_failure(monkeypatch):
    """A failed bind must still close the connection."""
    closed = []

    class FakeLDAPError(Exception):
        pass

    class FakeConn:
        def set_option(self, *a, **kw): pass
        def simple_bind_s(self, *a, **kw): raise FakeLDAPError("forced failure")
        def unbind_s(self): closed.append(True)

    class FakeLdapModule:
        OPT_REFERRALS = 1
        OPT_NETWORK_TIMEOUT = 2
        LDAPError = FakeLDAPError
        class INVALID_CREDENTIALS(FakeLDAPError): pass
        def initialize(self, url): return FakeConn()

    import sys
    monkeypatch.setitem(sys.modules, "ldap", FakeLdapModule())
    ok, detail = onboarding_probes._ldap_bind("home.gell.one", "x", "y")
    assert ok is False
    assert closed == [True], "unbind_s must be called even when bind raises"


def test_probe_tenant_validates_uuid_shape():
    result = onboarding_probes.probe_tenant("not-a-uuid", "contoso.onmicrosoft.com", graph_check=False)
    assert result["ok"] is False
    assert "Tenant id format" in result["detail"]


def test_probe_tenant_accepts_valid_uuid_when_graph_skipped():
    result = onboarding_probes.probe_tenant(
        "12345678-1234-1234-1234-123456789abc", "contoso.onmicrosoft.com", graph_check=False
    )
    assert result["ok"] is True


def test_probe_tenant_rejects_trailing_whitespace():
    """Pasting from the Entra portal can drag a trailing newline."""
    result = onboarding_probes.probe_tenant(
        "12345678-1234-1234-1234-123456789abc\n", "contoso.onmicrosoft.com", graph_check=False
    )
    assert result["ok"] is False
    assert "Tenant id format" in result["detail"]


def test_probe_artifact_lists_cache_contents(monkeypatch):
    """Inventory both CloudOSD and OSDeploy artifact stores.

    Deviation from plan text: the modules cloudosd_cache.py / osdeploy_cache.py
    exist but do NOT expose list_artifacts(); they manage cache *entries*
    (catalog downloads), not built artifacts. The canonical list-of-built-
    artifacts comes from cloudosd_pg.list_artifacts(conn) /
    osdeploy_pg.list_artifacts(conn), which the existing /artifacts endpoints
    already wrap. The probe monkeypatches those.
    """
    monkeypatch.setattr(
        "web.onboarding_probes._list_cloudosd_artifacts",
        lambda: [{"id": "cosd-1", "label": "CloudOSD 2026-05", "built_at": "2026-05-20T10:00Z"}],
    )
    monkeypatch.setattr(
        "web.onboarding_probes._list_osdeploy_artifacts",
        lambda: [{"id": "osd-1", "label": "OSDeploy 2026-05", "built_at": "2026-05-22T10:00Z"}],
    )
    result = onboarding_probes.probe_artifact()
    assert result["ok"] is True
    assert any(a["id"] == "cosd-1" for a in result["cloudosd"])
    assert any(a["id"] == "osd-1" for a in result["osdeploy"])


def test_probe_artifact_returns_empty_lists_when_db_unavailable(monkeypatch):
    """If the DB is not configured the probe still returns a usable shape."""
    def boom():
        raise RuntimeError("CloudOSD database is not configured")
    monkeypatch.setattr("web.onboarding_probes._list_cloudosd_artifacts", boom)
    monkeypatch.setattr("web.onboarding_probes._list_osdeploy_artifacts", boom)
    result = onboarding_probes.probe_artifact()
    assert result["ok"] is False
    assert result["cloudosd"] == []
    assert result["osdeploy"] == []
