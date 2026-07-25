"""Tests for the server-side CloudOSD AD join executor (cloudosd_domain_join)."""
import base64

import pytest

from web import cloudosd_domain_join as cdj


# --------------------------------------------------------------------------- #
# Pure helpers (no DB / no infra)
# --------------------------------------------------------------------------- #
def test_encode_powershell_is_utf16le_base64():
    enc = cdj.encode_powershell("Write-Output 'hi'")
    assert base64.b64decode(enc).decode("utf-16-le") == "Write-Output 'hi'"


@pytest.mark.parametrize("user,dj,expected", [
    ("adam", {"domain_fqdn": "test.gell.one"}, "adam@test.gell.one"),
    ("adam", {"credential_domain": "test.gell.one", "domain_fqdn": "ignored"}, "adam@test.gell.one"),
    ("TEST\\adam", {"domain_fqdn": "test.gell.one"}, "TEST\\adam"),
    ("adam@test.gell.one", {"domain_fqdn": "test.gell.one"}, "adam@test.gell.one"),
    ("adam", {}, "adam"),
    ("", {"domain_fqdn": "test.gell.one"}, ""),
])
def test_qualify_user(user, dj, expected):
    assert cdj.qualify_user(user, dj) == expected


def test_build_join_powershell_includes_domain_ou_credential_and_password_unescaped():
    ps = cdj.build_join_powershell(
        username="adam@test.gell.one",
        password="p@ss'w0rd\"x",
        domain="test.gell.one",
        ou_path="OU=Devices,DC=test,DC=gell,DC=one",
    )
    assert "Add-Computer -DomainName 'test.gell.one'" in ps
    assert "-OUPath 'OU=Devices,DC=test,DC=gell,DC=one'" in ps
    assert "PSCredential('adam@test.gell.one'" in ps
    # here-string means the raw password (quotes and all) appears verbatim
    assert "p@ss'w0rd\"x" in ps
    assert "JOIN_OK" in ps


def test_build_join_powershell_omits_ou_when_blank():
    assert "-OUPath" not in cdj.build_join_powershell(
        username="a", password="b", domain="d")


def test_probe_and_join_result_interpreters():
    assert cdj.probe_is_domain_joined({"out-data": "DOMAIN=True;NAME=test.gell.one"})
    assert not cdj.probe_is_domain_joined({"out-data": "DOMAIN=False;NAME=WORKGROUP"})
    assert not cdj.probe_is_domain_joined({})
    assert cdj.join_succeeded({"exitcode": 0, "out-data": "JOIN_OK"})
    assert not cdj.join_succeeded({"exitcode": 1, "out-data": "boom"})
    assert not cdj.join_succeeded({"exitcode": 0, "out-data": "nope"})


def test_guest_exec_via_proxmox_polls_until_exit():
    calls = []

    def post(path, data=None):
        calls.append(("post", path))
        return {"pid": 42}

    statuses = iter([{"exited": 0}, {"exited": 1, "exitcode": 0, "out-data": "JOIN_OK"}])

    def get(path):
        calls.append(("get", path))
        return next(statuses)

    result = cdj.guest_exec_via_proxmox(post, get, "pve2", 105, "Add-Computer",
                                        timeout=30, sleep=lambda s: None)
    assert result["out-data"] == "JOIN_OK"
    assert ("post", "/nodes/pve2/qemu/105/agent/exec") in calls
    assert sum(1 for kind, _ in calls if kind == "get") == 2


# --------------------------------------------------------------------------- #
# DB-backed candidate discovery + execution
# --------------------------------------------------------------------------- #
_DC_DOMAIN_JOIN = {
    "enabled": True,
    "credential_id": 286,
    "domain_fqdn": "test.gell.one",
    "credential_domain": "test.gell.one",
    "ou_path": "OU=Devices,DC=test,DC=gell,DC=one",
    "domain_controller_ipv4": "192.168.16.10",
    "acceptable_domain_names": ["test.gell.one"],
}

_HEARTBEAT_PREDECESSOR_KINDS = [
    "cloudosd_preflight", "cloudosd_deploy_os", "cloudosd_validate_offline_os",
    "stage_osd_client", "stage_autopilot_agent", "wait_agent_heartbeat",
]


def _init_db(pg_conn):
    from web import cloudosd_pg, sequences_pg, ts_engine_pg
    sequences_pg.reset_for_tests(pg_conn); sequences_pg.init(pg_conn)
    ts_engine_pg.reset_for_tests(pg_conn); ts_engine_pg.init(pg_conn)
    cloudosd_pg.reset_for_tests(pg_conn); cloudosd_pg.init(pg_conn)


def _make_artifact(pg_conn):
    from web import cloudosd_pg
    return cloudosd_pg.create_artifact(
        pg_conn,
        architecture="amd64",
        osdcloud_module_version="26.4.17.1",
        build_sha="cloudosdtest",
        iso_path="/app/output/cloudosd-autopilot-amd64-cloudosdtest.iso",
        wim_path="/app/output/cloudosd-autopilot-amd64-cloudosdtest.wim",
        manifest_path="/app/output/cloudosd-autopilot-amd64-cloudosdtest.json",
        iso_sha256="a" * 64,
        wim_sha256="b" * 64,
        built_by_host="tester@localhost",
        proxmox_volid="local:iso/cloudosd-autopilot-amd64-cloudosdtest.iso",
    )


def _make_run(pg_conn, *, domain_join, heartbeat_done=True, vmid=105, node="pve2"):
    from web import cloudosd_pg, ts_engine_pg
    artifact = _make_artifact(pg_conn)
    run = cloudosd_pg.create_run(
        pg_conn,
        artifact_id=artifact["id"],
        vm_name="ring0test-01",
        node=node,
        requested_vmid=vmid,
        domain_join=domain_join,
    )
    rid = run["run_id"]
    # Give the run a real vmid so the executor has a target.
    cloudosd_pg.set_run_identity(
        pg_conn, run_id=rid, vmid=vmid, vm_uuid="uuid-1",
        mac="AA:BB:CC:DD:EE:FF", node=node)
    if heartbeat_done:
        ts_engine_pg.mark_steps_done_by_kind(
            pg_conn, run_id=rid, kinds=_HEARTBEAT_PREDECESSOR_KINDS)
    return rid


def _step_state(pg_conn, run_id, kind):
    row = pg_conn.execute(
        "SELECT state FROM ts_run_plan_steps WHERE run_id = %s AND kind = %s",
        (run_id, kind),
    ).fetchone()
    return row["state"] if row else None


def _join_step_state(pg_conn, run_id):
    return _step_state(pg_conn, run_id, "join_domain_role")


def _hash_step_state(pg_conn, run_id):
    return _step_state(pg_conn, run_id, "capture_autopilot_hash")


def _verify_step_state(pg_conn, run_id):
    return _step_state(pg_conn, run_id, "verify_ad_domain_join")


class _FakeGuest:
    """Records guest_exec calls; answers probe/join/reboot by script content."""
    def __init__(self, *, probe_out="DOMAIN=False;NAME=WORKGROUP", join_ok=True, raise_on_probe=False):
        self.calls = []
        self.probe_out = probe_out
        self.join_ok = join_ok
        self.raise_on_probe = raise_on_probe

    def __call__(self, node, vmid, script):
        self.calls.append((node, vmid, script))
        if "PartOfDomain" in script:
            if self.raise_on_probe:
                raise RuntimeError("guest unreachable (rebooting)")
            return {"exited": 1, "exitcode": 0, "out-data": self.probe_out}
        if "Add-Computer" in script:
            if self.join_ok:
                return {"exited": 1, "exitcode": 0, "out-data": "JOIN_OK"}
            return {"exited": 1, "exitcode": 1, "err-data": "access denied"}
        return {"exited": 1, "exitcode": 0, "out-data": ""}

    @property
    def add_computer_scripts(self):
        return [s for _, _, s in self.calls if "Add-Computer" in s]


def _cred(_cid):
    return {"username": "adam", "password": "s3cret"}


def test_run_pending_joins_joins_and_marks_step_done(pg_conn):
    from web import cloudosd_pg
    _init_db(pg_conn)
    rid = _make_run(pg_conn, domain_join=_DC_DOMAIN_JOIN)
    guest = _FakeGuest(probe_out="DOMAIN=False;NAME=WORKGROUP", join_ok=True)

    summary = cdj.run_pending_joins(
        pg_conn, guest_exec=guest, resolve_credential=_cred,
        append_event=cloudosd_pg.append_event,
    )

    assert summary["candidates"] == 1
    assert summary["joined"] == 1
    assert _join_step_state(pg_conn, rid) == "done"
    # A fresh Add-Computer completes only the join; verify is confirmed on a
    # later tick once the guest has rebooted and PartOfDomain reads true.
    assert _verify_step_state(pg_conn, rid) == "pending"
    # Issued exactly one Add-Computer, targeted at the right domain/OU/user.
    assert len(guest.add_computer_scripts) == 1
    join_ps = guest.add_computer_scripts[0]
    assert "-DomainName 'test.gell.one'" in join_ps
    assert "-OUPath 'OU=Devices,DC=test,DC=gell,DC=one'" in join_ps
    assert "PSCredential('adam@test.gell.one'" in join_ps
    # A reboot followed the join.
    assert any("Restart-Computer" in s for _, _, s in guest.calls)
    events = {e["event_type"] for e in cloudosd_pg.list_events(pg_conn, rid)}
    assert "domain_join_executed" in events


def test_already_joined_marks_done_without_add_computer(pg_conn):
    from web import cloudosd_pg
    _init_db(pg_conn)
    rid = _make_run(pg_conn, domain_join=_DC_DOMAIN_JOIN)
    guest = _FakeGuest(probe_out="DOMAIN=True;NAME=test.gell.one")

    summary = cdj.run_pending_joins(
        pg_conn, guest_exec=guest, resolve_credential=_cred,
        append_event=cloudosd_pg.append_event,
    )

    assert summary["already_joined"] == 1
    # A confirmed-in-domain probe completes BOTH membership steps and emits the
    # verified evidence, so the run can reach completion without an osd_v2 agent.
    assert _join_step_state(pg_conn, rid) == "done"
    assert _verify_step_state(pg_conn, rid) == "done"
    assert guest.add_computer_scripts == []
    events = {e["event_type"] for e in cloudosd_pg.list_events(pg_conn, rid)}
    assert "domain_join_verified" in events


def test_not_a_candidate_until_heartbeat_predecessor_done(pg_conn):
    _init_db(pg_conn)
    rid = _make_run(pg_conn, domain_join=_DC_DOMAIN_JOIN, heartbeat_done=False)
    guest = _FakeGuest()

    summary = cdj.run_pending_joins(pg_conn, guest_exec=guest, resolve_credential=_cred)

    assert summary["candidates"] == 0
    assert _join_step_state(pg_conn, rid) == "pending"
    assert guest.calls == []


def test_no_dc_ip_compiles_no_join_role_step_and_is_not_a_candidate(pg_conn):
    _init_db(pg_conn)
    dj = dict(_DC_DOMAIN_JOIN)
    dj.pop("domain_controller_ipv4")
    rid = _make_run(pg_conn, domain_join=dj)
    guest = _FakeGuest()

    summary = cdj.run_pending_joins(pg_conn, guest_exec=guest, resolve_credential=_cred)

    assert summary["candidates"] == 0
    # The offline-unattend path compiles no join_domain_role step at all.
    assert _join_step_state(pg_conn, rid) is None


def test_unreachable_guest_leaves_step_pending_for_retry(pg_conn):
    _init_db(pg_conn)
    rid = _make_run(pg_conn, domain_join=_DC_DOMAIN_JOIN)
    guest = _FakeGuest(raise_on_probe=True)

    summary = cdj.run_pending_joins(pg_conn, guest_exec=guest, resolve_credential=_cred)

    assert summary["unreachable"] == 1
    assert summary["joined"] == 0
    assert _join_step_state(pg_conn, rid) == "pending"


def test_advance_domain_joined_runs_uses_injected_completion(pg_conn):
    """A run waiting on domain join is driven forward when a matching heartbeat
    is available; the completion callback is invoked with that heartbeat."""
    from web import cloudosd_pg
    _init_db(pg_conn)
    rid = _make_run(pg_conn, domain_join=_DC_DOMAIN_JOIN)
    # Park the run in the waiting state the advancer targets.
    pg_conn.execute(
        "UPDATE cloudosd_runs SET state = 'full_os_waiting_domain_join' WHERE run_id = %s",
        (rid,),
    )
    pg_conn.commit()

    calls = []

    def latest_heartbeat(run_id):
        return {"received_at": "now", "domain_joined": True, "domain_name": "test.gell.one"}

    def mark_complete(conn, *, run_id, heartbeat_at, heartbeat):
        calls.append((run_id, heartbeat_at, heartbeat["domain_name"]))
        conn.execute(
            "UPDATE cloudosd_runs SET state = 'complete' WHERE run_id = %s",
            (run_id,),
        )
        conn.commit()
        return {"state": "complete"}

    result = cdj.advance_domain_joined_runs(
        pg_conn, latest_heartbeat=latest_heartbeat, mark_complete=mark_complete)

    assert result == {"waiting": 1, "advanced": 1}
    assert calls == [(rid, "now", "test.gell.one")]


def test_advance_skips_runs_without_heartbeat(pg_conn):
    from web import cloudosd_pg
    _init_db(pg_conn)
    rid = _make_run(pg_conn, domain_join=_DC_DOMAIN_JOIN)
    pg_conn.execute(
        "UPDATE cloudosd_runs SET state = 'full_os_waiting_domain_join' WHERE run_id = %s",
        (rid,),
    )
    pg_conn.commit()

    def mark_complete(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("mark_complete should not run without a heartbeat")

    result = cdj.advance_domain_joined_runs(
        pg_conn, latest_heartbeat=lambda rid: None, mark_complete=mark_complete)

    assert result == {"waiting": 1, "advanced": 0}


def test_failed_join_leaves_step_pending_and_records_event(pg_conn):
    from web import cloudosd_pg
    _init_db(pg_conn)
    rid = _make_run(pg_conn, domain_join=_DC_DOMAIN_JOIN)
    guest = _FakeGuest(join_ok=False)

    summary = cdj.run_pending_joins(
        pg_conn, guest_exec=guest, resolve_credential=_cred,
        append_event=cloudosd_pg.append_event,
    )

    assert summary["failed"] == 1
    assert _join_step_state(pg_conn, rid) == "pending"
    events = {e["event_type"] for e in cloudosd_pg.list_events(pg_conn, rid)}
    assert "domain_join_failed" in events


# --------------------------------------------------------------------------- #
# Heartbeat-recency guard
# --------------------------------------------------------------------------- #
def test_heartbeat_is_stale_helper():
    from datetime import datetime, timezone, timedelta
    now = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)
    assert cdj._heartbeat_is_stale(None, now, 1800)
    assert cdj._heartbeat_is_stale({"received_at": now - timedelta(hours=1)}, now, 1800)
    assert not cdj._heartbeat_is_stale({"received_at": now - timedelta(seconds=60)}, now, 1800)
    # naive timestamp is treated as UTC, not crashed on
    assert not cdj._heartbeat_is_stale(
        {"received_at": (now - timedelta(seconds=30)).replace(tzinfo=None)}, now, 1800)


def test_join_skips_run_with_no_heartbeat(pg_conn):
    _init_db(pg_conn)
    rid = _make_run(pg_conn, domain_join=_DC_DOMAIN_JOIN)
    guest = _FakeGuest()
    summary = cdj.run_pending_joins(
        pg_conn, guest_exec=guest, resolve_credential=_cred,
        latest_heartbeat=lambda run_id: None)
    assert summary["stale"] == 1
    assert summary["joined"] == 0
    assert guest.calls == []  # a dead run is never probed
    assert _join_step_state(pg_conn, rid) == "pending"


# --------------------------------------------------------------------------- #
# Autopilot hardware-hash capture
# --------------------------------------------------------------------------- #
def test_parse_hash_output():
    assert cdj.parse_hash_output(
        {"out-data": "SERIAL=ABC123\r\nHASH=T0RhdGFoYXNo\r\n"}) == ("ABC123", "T0RhdGFoYXNo")
    assert cdj.parse_hash_output({"out-data": ""}) == ("", "")
    assert cdj.parse_hash_output({}) == ("", "")


def _hash_guest(out):
    def guest(node, vmid, script):
        assert "DeviceHardwareData" in script
        return {"exitcode": 0, "out-data": out}
    return guest


def test_hash_capture_persists_and_marks_step_done(pg_conn):
    from web import cloudosd_pg
    _init_db(pg_conn)
    rid = _make_run(pg_conn, domain_join=_DC_DOMAIN_JOIN)
    captured = []

    def persist_hash(**kw):
        captured.append(kw)
        return "hwid.csv"

    summary = cdj.run_pending_hash_captures(
        pg_conn, guest_exec=_hash_guest("SERIAL=SN-1\r\nHASH=BASE64HASH=="),
        persist_hash=persist_hash, append_event=cloudosd_pg.append_event)

    assert summary["captured"] == 1
    assert _hash_step_state(pg_conn, rid) == "done"
    assert captured[0]["serial"] == "SN-1"
    assert captured[0]["hardware_hash"] == "BASE64HASH=="
    events = {e["event_type"] for e in cloudosd_pg.list_events(pg_conn, rid)}
    assert "autopilot_hash_captured" in events


def test_hash_capture_fails_on_empty_hash_and_leaves_step_pending(pg_conn):
    _init_db(pg_conn)
    rid = _make_run(pg_conn, domain_join=_DC_DOMAIN_JOIN)

    def persist_hash(**kw):  # pragma: no cover - must not run on empty hash
        raise AssertionError("should not persist an empty hash")

    summary = cdj.run_pending_hash_captures(
        pg_conn, guest_exec=_hash_guest("SERIAL=SN-1\r\nHASH="),
        persist_hash=persist_hash)

    assert summary["failed"] == 1
    assert _hash_step_state(pg_conn, rid) == "pending"


def test_hash_capture_skips_stale_run(pg_conn):
    _init_db(pg_conn)
    rid = _make_run(pg_conn, domain_join=_DC_DOMAIN_JOIN)
    called = []

    def guest(*a):
        called.append(a)
        return {}

    summary = cdj.run_pending_hash_captures(
        pg_conn, guest_exec=guest, persist_hash=lambda **k: None,
        latest_heartbeat=lambda run_id: None)

    assert summary["stale"] == 1
    assert called == []
    assert _hash_step_state(pg_conn, rid) == "pending"


# --------------------------------------------------------------------------- #
# advance covers full_os_waiting_v2
# --------------------------------------------------------------------------- #
def test_advance_covers_full_os_waiting_v2(pg_conn):
    _init_db(pg_conn)
    rid = _make_run(pg_conn, domain_join=_DC_DOMAIN_JOIN)
    pg_conn.execute(
        "UPDATE cloudosd_runs SET state = 'full_os_waiting_v2' WHERE run_id = %s", (rid,))
    pg_conn.commit()
    seen = []

    def mark_complete(conn, *, run_id, heartbeat_at, heartbeat):
        seen.append(run_id)
        conn.execute(
            "UPDATE cloudosd_runs SET state = 'complete' WHERE run_id = %s", (run_id,))
        conn.commit()
        return {"state": "complete"}

    result = cdj.advance_domain_joined_runs(
        pg_conn, latest_heartbeat=lambda r: {"received_at": "now"}, mark_complete=mark_complete)

    assert result == {"waiting": 1, "advanced": 1}
    assert seen == [rid]


# --------------------------------------------------------------------------- #
# Executor owns verify_ad_domain_join: a run whose join is already done but
# verify is still pending is re-probed and verified (the just-joined-then-
# rebooted case), without any osd_v2 agent.
# --------------------------------------------------------------------------- #
def test_run_with_join_done_and_verify_pending_gets_verified(pg_conn):
    from web import cloudosd_pg, ts_engine_pg
    _init_db(pg_conn)
    rid = _make_run(pg_conn, domain_join=_DC_DOMAIN_JOIN)
    ts_engine_pg.mark_steps_done_by_kind(pg_conn, run_id=rid, kinds=["join_domain_role"])
    assert _verify_step_state(pg_conn, rid) == "pending"
    guest = _FakeGuest(probe_out="DOMAIN=True;NAME=test.gell.one")

    summary = cdj.run_pending_joins(
        pg_conn, guest_exec=guest, resolve_credential=_cred,
        append_event=cloudosd_pg.append_event)

    # Still a candidate (verify pending); probe confirms membership -> verify done.
    assert summary["candidates"] == 1
    assert summary["already_joined"] == 1
    assert _verify_step_state(pg_conn, rid) == "done"
    assert guest.add_computer_scripts == []  # already joined, no re-join
    events = {e["event_type"] for e in cloudosd_pg.list_events(pg_conn, rid)}
    assert "domain_join_verified" in events
