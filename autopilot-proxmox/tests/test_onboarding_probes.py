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
