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
