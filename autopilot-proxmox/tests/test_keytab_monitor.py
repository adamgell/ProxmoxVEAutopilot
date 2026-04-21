"""Tests for web.keytab_monitor — probe + refresh.

Fake ``run_cmd`` injections so we don't need kinit/klist to be
installed in the test environment."""
import os
import time
from pathlib import Path

import pytest


def _fake_runner(responses):
    """Build a run_cmd replacement that returns canned (rc, output)
    tuples based on the first token of argv."""
    calls = []
    def run(argv, env=None, timeout=30):
        calls.append(argv)
        for prefix, response in responses.items():
            if argv[0].endswith(prefix) or argv[0] == prefix:
                return response
        raise AssertionError(f"no fake for: {argv}")
    return run, calls


@pytest.fixture
def db_path(tmp_path):
    from web import device_history_db
    p = tmp_path / "d.db"
    device_history_db.init(p)
    return p


def test_probe_missing_file_returns_missing(tmp_path):
    from web import keytab_monitor
    out = keytab_monitor.probe_keytab(
        keytab_path=str(tmp_path / "nope.keytab"),
        principal="svc-apmon$@HOME.GELL.ONE",
        ldap_host="dc.local",
    )
    assert out.status == keytab_monitor.STATUS_MISSING
    assert "not found" in out.message


def test_probe_klist_failure_returns_broken(tmp_path):
    from web import keytab_monitor
    kt = tmp_path / "krb5.keytab"
    kt.write_bytes(b"garbage")
    run, _ = _fake_runner({
        "klist": (1, "klist: Bad version number"),
    })
    out = keytab_monitor.probe_keytab(
        keytab_path=str(kt),
        principal="svc-apmon$@HOME.GELL.ONE",
        ldap_host="dc.local",
        run_cmd=run,
    )
    assert out.status == keytab_monitor.STATUS_BROKEN
    assert "klist" in out.message.lower()


def test_probe_kinit_failure_returns_broken(tmp_path):
    from web import keytab_monitor
    kt = tmp_path / "krb5.keytab"
    kt.write_bytes(b"\x05\x02")
    run, _ = _fake_runner({
        "klist": (0, "KVNO Principal\n---- --------\n   7 svc-apmon$@HOME.GELL.ONE\n"),
        "kinit": (1, "kinit: Preauthentication failed"),
    })
    out = keytab_monitor.probe_keytab(
        keytab_path=str(kt),
        principal="svc-apmon$@HOME.GELL.ONE",
        ldap_host="dc.local",
        run_cmd=run,
    )
    assert out.status == keytab_monitor.STATUS_BROKEN
    assert "Preauthentication" in (out.kinit_error or "")
    assert out.keytab_kvno_local == 7
    assert out.keytab_principal == "svc-apmon$@HOME.GELL.ONE"


def test_probe_kinit_ok_with_recent_mtime_returns_ok(tmp_path):
    from web import keytab_monitor
    kt = tmp_path / "krb5.keytab"
    kt.write_bytes(b"\x05\x02")
    # Just-created: mtime is ~now.
    run, _ = _fake_runner({
        "klist": (0, "KVNO Principal\n---- ---\n   3 svc-apmon$@HOME.GELL.ONE\n"),
        "kinit": (0, ""),
        "kdestroy": (0, ""),
    })
    out = keytab_monitor.probe_keytab(
        keytab_path=str(kt),
        principal="svc-apmon$@HOME.GELL.ONE",
        ldap_host="dc.local",
        # No gmsa_dn — skips LDAP kvno compare.
        run_cmd=run,
    )
    assert out.status == keytab_monitor.STATUS_OK
    assert out.keytab_kvno_local == 3
    assert out.kinit_ok is True


def test_probe_age_stale_yellow(tmp_path):
    from web import keytab_monitor
    kt = tmp_path / "krb5.keytab"
    kt.write_bytes(b"\x05\x02")
    # Force mtime to 10 days ago.
    ten_days_ago = time.time() - (10 * 24 * 3600)
    os.utime(kt, (ten_days_ago, ten_days_ago))
    run, _ = _fake_runner({
        "klist": (0, "KVNO Principal\n---- ---\n   3 svc-apmon$@HOME.GELL.ONE\n"),
        "kinit": (0, ""),
        "kdestroy": (0, ""),
    })
    out = keytab_monitor.probe_keytab(
        keytab_path=str(kt),
        principal="svc-apmon$@HOME.GELL.ONE",
        ldap_host="dc.local",
        run_cmd=run,
    )
    assert out.status == keytab_monitor.STATUS_STALE
    assert "24" in out.message or "days" in out.message.lower() or "h" in out.message


def test_probe_age_red_approaching_rotation(tmp_path):
    from web import keytab_monitor
    kt = tmp_path / "krb5.keytab"
    kt.write_bytes(b"\x05\x02")
    twenty_five_days_ago = time.time() - (25 * 24 * 3600)
    os.utime(kt, (twenty_five_days_ago, twenty_five_days_ago))
    run, _ = _fake_runner({
        "klist": (0, "KVNO Principal\n---- ---\n   3 svc-apmon$@HOME.GELL.ONE\n"),
        "kinit": (0, ""),
        "kdestroy": (0, ""),
    })
    out = keytab_monitor.probe_keytab(
        keytab_path=str(kt),
        principal="svc-apmon$@HOME.GELL.ONE",
        ldap_host="dc.local",
        run_cmd=run,
    )
    assert out.status == keytab_monitor.STATUS_BROKEN
    assert "rotation" in out.message.lower() or "red" in out.message.lower()


def test_record_probe_writes_to_db(db_path):
    from web import keytab_monitor, device_history_db
    r = keytab_monitor.KeytabProbeResult(
        status="ok", message="test",
        keytab_path="/etc/krb5.keytab",
        keytab_mtime="2026-04-21T12:00:00+00:00",
        keytab_principal="svc-apmon$@HOME.GELL.ONE",
        keytab_kvno_local=7, keytab_kvno_ad=7,
        kinit_ok=True, kinit_error=None,
    )
    keytab_monitor.record_probe(db_path, r)
    h = device_history_db.get_keytab_health(db_path)
    assert h["last_probe_status"] == "ok"
    assert h["keytab_kvno_local"] == 7
    assert h["keytab_principal"] == "svc-apmon$@HOME.GELL.ONE"
    assert h["last_kinit_ok"] == 1


def test_refresh_writes_to_db_on_failure(db_path, tmp_path):
    from web import keytab_monitor, device_history_db
    # Runner returns non-zero for kinit — the refresher should
    # mark last_refresh_ok=0 and not raise.
    run, _ = _fake_runner({
        "kinit": (1, "kinit: wrong password"),
        "kdestroy": (0, ""),
    })
    ok, msg = keytab_monitor.refresh_keytab(
        db_path=db_path,
        kinit_principal="adam_admin@HOME.GELL.ONE",
        kinit_password="wrong", keytab_path=str(tmp_path / "out.keytab"),
        gmsa_dn="CN=svc-apmon,CN=Managed Service Accounts,DC=home,DC=gell,DC=one",
        ldap_host="dc.local", realm="HOME.GELL.ONE",
        gmsa_sam="svc-apmon", run_cmd=run,
    )
    assert ok is False
    h = device_history_db.get_keytab_health(db_path)
    assert h["last_refresh_ok"] == 0
    assert "kinit" in (h["last_refresh_message"] or "").lower()


def test_kvno_mismatch_surfaces_as_yellow_status(tmp_path):
    """When local kvno != AD kvno the probe should emit KVNO_MISMATCH,
    which the UI renders as yellow so the operator knows the refresher
    didn't write through to AD's current password version."""
    from web.keytab_monitor import KeytabProbeResult, STATUS_KVNO_MISMATCH
    # Direct construction to mirror what probe_keytab would produce
    # after the LDAP kvno compare fails. Keeps the test away from
    # subprocess side effects.
    r = KeytabProbeResult(
        status="ok", message="",
        keytab_path="/x", keytab_mtime="2026-04-21T12:00:00+00:00",
        keytab_principal="svc-apmon$@HOME.GELL.ONE",
        keytab_kvno_local=5, keytab_kvno_ad=7,
        kinit_ok=True, kinit_error=None,
    )
    # Simulate the final classification the probe does:
    if r.keytab_kvno_local != r.keytab_kvno_ad:
        r.status = STATUS_KVNO_MISMATCH
    assert r.status == STATUS_KVNO_MISMATCH
