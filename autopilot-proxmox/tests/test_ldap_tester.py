"""Tests for web.ldap_tester — validates domain_join credentials via ldap3."""
from unittest.mock import MagicMock, patch

import pytest


def _payload(**over):
    return {"domain_fqdn": "example.local",
            "username": "EXAMPLE\\svc_join",
            "password": "secret",
            "ou_hint": over.pop("ou_hint", ""),
            **over}


def test_returns_ok_structure_on_full_success():
    from web import ldap_tester
    with patch("web.ldap_tester._dns_srv_lookup") as mock_dns, \
         patch("web.ldap_tester._try_bind") as mock_bind:
        mock_dns.return_value = (["dc01.example.local"], 8)
        # mock_bind returns (connect_result, bind_result, rootdse_result)
        mock_bind.return_value = (
            {"ok": True, "server": "dc01.example.local", "tls": "ldaps",
             "elapsed_ms": 48},
            {"ok": True, "elapsed_ms": 61},
            {"ok": True, "defaultNamingContext": "DC=example,DC=local",
             "dnsHostName": "dc01.example.local"},
        )
        out = ldap_tester.test_domain_join(_payload(), validate_certs=False)
    assert out["ok"] is True
    assert out["dns"]["ok"] is True
    assert out["dns"]["servers"] == ["dc01.example.local"]
    assert out["connect"]["ok"] is True
    assert out["bind"]["ok"] is True
    assert out["rootdse"]["ok"] is True
    assert out["ou"]["ok"] is True  # empty ou_hint → skipped, reports ok


def test_reports_dns_failure_and_stops():
    from web import ldap_tester
    with patch("web.ldap_tester._dns_srv_lookup", side_effect=Exception("NXDOMAIN")):
        out = ldap_tester.test_domain_join(_payload(), validate_certs=False)
    assert out["ok"] is False
    assert out["dns"]["ok"] is False
    assert "NXDOMAIN" in out["dns"]["error"]
    # Later stages should not have tried to run
    assert "bind" not in out or out["bind"].get("ok") is not True


def test_reports_bind_failure_with_ldap_error_text():
    from web import ldap_tester
    with patch("web.ldap_tester._dns_srv_lookup") as mock_dns, \
         patch("web.ldap_tester._try_bind") as mock_bind:
        mock_dns.return_value = (["dc01.example.local"], 8)
        mock_bind.return_value = (
            {"ok": True, "server": "dc01.example.local", "tls": "ldaps",
             "elapsed_ms": 48},
            {"ok": False, "elapsed_ms": 40, "error": "invalidCredentials"},
            None,
        )
        out = ldap_tester.test_domain_join(_payload(), validate_certs=False)
    assert out["ok"] is False
    assert out["bind"]["error"] == "invalidCredentials"


def test_password_never_echoed_in_response():
    from web import ldap_tester
    with patch("web.ldap_tester._dns_srv_lookup", side_effect=Exception("no dns")):
        out = ldap_tester.test_domain_join(_payload(password="HUNT3R-42"),
                                            validate_certs=False)
    # Walk the whole response — the password must NOT appear anywhere.
    import json
    assert "HUNT3R-42" not in json.dumps(out)


def test_ou_search_runs_when_ou_hint_supplied():
    from web import ldap_tester
    with patch("web.ldap_tester._dns_srv_lookup") as mock_dns, \
         patch("web.ldap_tester._try_bind") as mock_bind, \
         patch("web.ldap_tester._search_ou") as mock_ou:
        mock_dns.return_value = (["dc01.example.local"], 8)
        mock_bind.return_value = (
            {"ok": True, "server": "dc01.example.local", "tls": "ldaps",
             "elapsed_ms": 48},
            {"ok": True, "elapsed_ms": 61},
            {"ok": True, "defaultNamingContext": "DC=example,DC=local",
             "dnsHostName": "dc01.example.local"},
        )
        mock_ou.return_value = {"ok": True, "dn": "OU=X,DC=example,DC=local",
                                 "elapsed_ms": 37}
        out = ldap_tester.test_domain_join(
            _payload(ou_hint="OU=X,DC=example,DC=local"), validate_certs=False)
    assert out["ou"]["ok"] is True
    assert out["ou"]["dn"] == "OU=X,DC=example,DC=local"
    mock_ou.assert_called_once()
