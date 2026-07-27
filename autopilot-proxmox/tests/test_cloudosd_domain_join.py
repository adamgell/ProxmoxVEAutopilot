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


def test_build_join_powershell_includes_domain_ou_and_credential():
    ps = cdj.build_join_powershell(
        username="adam@test.gell.one",
        password="p@ss'w0rd\"x",
        domain="test.gell.one",
        ou_path="OU=Devices,DC=test,DC=gell,DC=one",
    )
    assert "Add-Computer -DomainName 'test.gell.one'" in ps
    assert "-OUPath 'OU=Devices,DC=test,DC=gell,DC=one'" in ps
    assert "PSCredential('adam@test.gell.one'" in ps
    assert "JOIN_OK" in ps
    # The password is never interpolated as script text, so nothing in it can
    # terminate a literal or otherwise escape into the script.
    assert "p@ss'w0rd\"x" not in ps


@pytest.mark.parametrize("password", [
    "p@ss'w0rd\"x",
    "quote'and'@more",
    "'@\nnot-a-here-string-terminator",
    "plain",
])
def test_build_join_powershell_round_trips_any_password_as_base64(password):
    ps = cdj.build_join_powershell(
        username="a", password=password, domain="d")
    blob = ps.split("FromBase64String('")[1].split("')")[0]
    assert base64.b64decode(blob).decode("utf-16-le") == password


def test_build_join_powershell_escapes_single_quotes():
    """An apostrophe in an OU path (OU=O'Brien,...) is ordinary and must not
    terminate the PowerShell literal it sits inside."""
    ps = cdj.build_join_powershell(
        username="o'neill@test.gell.one",
        password="pw",
        domain="o'domain.test",
        ou_path="OU=O'Brien,DC=test",
    )
    assert "-OUPath 'OU=O''Brien,DC=test'" in ps
    assert "PSCredential('o''neill@test.gell.one'" in ps
    assert "-DomainName 'o''domain.test'" in ps
    # Every literal is balanced: an odd count would mean one leaked out.
    assert ps.count("'") % 2 == 0


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


def test_guest_exec_via_proxmox_raises_on_timeout():
    """A never-exiting exec raises rather than returning a sentinel dict every
    caller would have to recognise; callers already treat raises as unreachable."""
    with pytest.raises(cdj.GuestExecTimeout):
        cdj.guest_exec_via_proxmox(
            lambda path, data=None: {"pid": 42},
            lambda path: {"exited": 0},
            "pve2", 105, "Restart-Computer",
            timeout=0, sleep=lambda s: None,
        )


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
    from web import agent_telemetry_pg, cloudosd_pg, sequences_pg, ts_engine_pg
    sequences_pg.reset_for_tests(pg_conn); sequences_pg.init(pg_conn)
    ts_engine_pg.reset_for_tests(pg_conn); ts_engine_pg.init(pg_conn)
    cloudosd_pg.reset_for_tests(pg_conn); cloudosd_pg.init(pg_conn)
    # The candidate queries join agent_heartbeats to order live runs first.
    agent_telemetry_pg.reset_for_tests(pg_conn); agent_telemetry_pg.init(pg_conn)


def _make_artifact(pg_conn, *, build_sha="cloudosdtest"):
    from web import cloudosd_pg
    return cloudosd_pg.create_artifact(
        pg_conn,
        architecture="amd64",
        osdcloud_module_version="26.4.17.1",
        build_sha=build_sha,
        iso_path=f"/app/output/cloudosd-autopilot-amd64-{build_sha}.iso",
        wim_path=f"/app/output/cloudosd-autopilot-amd64-{build_sha}.wim",
        manifest_path=f"/app/output/cloudosd-autopilot-amd64-{build_sha}.json",
        iso_sha256="a" * 64,
        wim_sha256="b" * 64,
        built_by_host="tester@localhost",
        proxmox_volid=f"local:iso/cloudosd-autopilot-amd64-{build_sha}.iso",
    )


def _make_run(pg_conn, *, domain_join, heartbeat_done=True, vmid=105, node="pve2",
              vm_name="ring0test-01"):
    from web import cloudosd_pg, ts_engine_pg
    artifact = _make_artifact(pg_conn, build_sha=f"sha{vmid}")
    run = cloudosd_pg.create_run(
        pg_conn,
        artifact_id=artifact["id"],
        vm_name=vm_name,
        node=node,
        requested_vmid=vmid,
        domain_join=domain_join,
    )
    rid = run["run_id"]
    # Give the run a real vmid so the executor has a target.
    cloudosd_pg.set_run_identity(
        pg_conn, run_id=rid, vmid=vmid, vm_uuid=f"uuid-{vmid}",
        mac="AA:BB:CC:DD:EE:FF", node=node)
    if heartbeat_done:
        ts_engine_pg.mark_steps_done_by_kind(
            pg_conn, run_id=rid, kinds=_HEARTBEAT_PREDECESSOR_KINDS)
    return rid


def _add_heartbeat(pg_conn, run_id, *, age_seconds=0.0, agent_id=None):
    """Insert a real agent_heartbeats row for a run, optionally back-dated."""
    from datetime import datetime, timedelta, timezone
    from web import agent_telemetry_pg
    agent_id = agent_id or f"agent-{run_id}"
    agent_telemetry_pg.upsert_device(pg_conn, agent_id=agent_id, token=f"tok-{agent_id}")
    pg_conn.execute(
        "INSERT INTO agent_heartbeats (agent_id, received_at, current_run_id)"
        " VALUES (%s, %s, %s)",
        (agent_id, datetime.now(timezone.utc) - timedelta(seconds=age_seconds), run_id),
    )
    pg_conn.commit()


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
        self.timeouts = []
        self.probe_out = probe_out
        self.join_ok = join_ok
        self.raise_on_probe = raise_on_probe

    def __call__(self, node, vmid, script, timeout=None):
        self.calls.append((node, vmid, script))
        self.timeouts.append(timeout)
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

    assert result == {"waiting": 1, "advanced": 1, "stale": 0}
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

    assert result == {"waiting": 1, "advanced": 0, "stale": 0}


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
    cutoff = cdj.heartbeat_cutoff(1800, now)
    assert cutoff == now - timedelta(seconds=1800)
    assert cdj._heartbeat_is_stale(None, cutoff)
    assert cdj._heartbeat_is_stale(now - timedelta(hours=1), cutoff)
    assert not cdj._heartbeat_is_stale(now - timedelta(seconds=60), cutoff)
    # naive timestamp is treated as UTC, not crashed on
    assert not cdj._heartbeat_is_stale(
        (now - timedelta(seconds=30)).replace(tzinfo=None), cutoff)
    # No cutoff configured -> the gate is off and nothing is stale.
    assert cdj.heartbeat_cutoff(None, now) is None
    assert not cdj._heartbeat_is_stale(None, None)


def test_heartbeat_is_stale_fails_closed_on_unexpected_shape():
    """An unparseable timestamp counts as stale. Treating it as live would
    silently disable the guard the moment the column shape changed."""
    from datetime import datetime, timezone
    cutoff = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)
    assert cdj._heartbeat_is_stale("2026-07-24T12:30:00Z", cutoff)
    assert cdj._heartbeat_is_stale(object(), cutoff)


def test_join_skips_run_with_no_heartbeat(pg_conn):
    _init_db(pg_conn)
    rid = _make_run(pg_conn, domain_join=_DC_DOMAIN_JOIN)
    guest = _FakeGuest()
    summary = cdj.run_pending_joins(
        pg_conn, guest_exec=guest, resolve_credential=_cred,
        max_heartbeat_age_seconds=1800)
    assert summary["stale"] == 1
    assert summary["joined"] == 0
    assert guest.calls == []  # a dead run is never probed
    assert _join_step_state(pg_conn, rid) == "pending"


def test_join_skips_run_whose_heartbeat_is_too_old(pg_conn):
    _init_db(pg_conn)
    rid = _make_run(pg_conn, domain_join=_DC_DOMAIN_JOIN)
    _add_heartbeat(pg_conn, rid, age_seconds=7200)
    guest = _FakeGuest()

    summary = cdj.run_pending_joins(
        pg_conn, guest_exec=guest, resolve_credential=_cred,
        max_heartbeat_age_seconds=1800)

    assert summary["stale"] == 1
    assert guest.calls == []


def test_join_runs_a_candidate_with_a_fresh_heartbeat(pg_conn):
    _init_db(pg_conn)
    rid = _make_run(pg_conn, domain_join=_DC_DOMAIN_JOIN)
    _add_heartbeat(pg_conn, rid, age_seconds=30)
    guest = _FakeGuest()

    summary = cdj.run_pending_joins(
        pg_conn, guest_exec=guest, resolve_credential=_cred,
        max_heartbeat_age_seconds=1800)

    assert summary["stale"] == 0
    assert summary["joined"] == 1
    assert _join_step_state(pg_conn, rid) == "done"


def test_live_runs_are_ordered_ahead_of_stale_ones(pg_conn):
    """The freshness check used to run in Python, i.e. after the SQL LIMIT had
    already been spent on the oldest - and therefore deadest - rows. A backlog
    of abandoned runs must not starve a live one out of the limit."""
    _init_db(pg_conn)
    dead = [
        _make_run(pg_conn, domain_join=_DC_DOMAIN_JOIN, vmid=200 + i,
                  vm_name=f"ring0dead-{i:02d}")
        for i in range(3)
    ]
    for rid in dead:
        _add_heartbeat(pg_conn, rid, age_seconds=7200)
    live = _make_run(pg_conn, domain_join=_DC_DOMAIN_JOIN, vmid=300,
                     vm_name="ring0live-01")
    _add_heartbeat(pg_conn, live, age_seconds=30)

    cutoff = cdj.heartbeat_cutoff(1800, cdj._utcnow())
    top = cdj.find_join_candidates(pg_conn, limit=1, cutoff=cutoff)

    assert [c["run_id"] for c in top] == [live]


def test_join_sweep_respects_its_time_budget(pg_conn):
    """Each probe can block for the guest_exec timeout, so a sweep over a large
    backlog must stop at its budget instead of overrunning the tick."""
    _init_db(pg_conn)
    for i in range(3):
        _make_run(pg_conn, domain_join=_DC_DOMAIN_JOIN, vmid=400 + i,
                  vm_name=f"ring0budget-{i:02d}")
    guest = _FakeGuest()
    ticks = iter([0.0, 0.0, 99.0, 99.0])

    summary = cdj.run_pending_joins(
        pg_conn, guest_exec=guest, resolve_credential=_cred,
        max_seconds=10.0, monotonic=lambda: next(ticks))

    assert summary["candidates"] == 3
    assert summary["joined"] == 1
    assert summary["deferred"] == 2
    assert len(guest.add_computer_scripts) == 1


def test_missing_credential_id_reports_no_credential(pg_conn):
    """credential_id is nullable on the run row; int(None) used to raise past
    the no_credential branch and surface as an opaque per-tick 'failed'."""
    from web import cloudosd_pg
    _init_db(pg_conn)
    dj = dict(_DC_DOMAIN_JOIN)
    dj.pop("credential_id")
    rid = _make_run(pg_conn, domain_join=dj)

    def _boom(_cid):  # pragma: no cover - must not be reached
        raise AssertionError("credential lookup should not run without an id")

    summary = cdj.run_pending_joins(
        pg_conn, guest_exec=_FakeGuest(), resolve_credential=_boom,
        append_event=cloudosd_pg.append_event)

    assert summary["failed"] == 0
    assert summary["skipped"] == 1
    assert summary["results"][0]["status"] == "no_credential"
    assert _join_step_state(pg_conn, rid) == "pending"
    events = {e["event_type"] for e in cloudosd_pg.list_events(pg_conn, rid)}
    assert "domain_join_failed" in events


def test_reboot_after_join_uses_a_short_timeout(pg_conn):
    """The guest is on its way down, so waiting the full exec timeout for a
    reboot to 'exit' would burn the tick."""
    _init_db(pg_conn)
    _make_run(pg_conn, domain_join=_DC_DOMAIN_JOIN)
    guest = _FakeGuest()

    cdj.run_pending_joins(pg_conn, guest_exec=guest, resolve_credential=_cred)

    reboot_timeouts = [
        t for (_, _, script), t in zip(guest.calls, guest.timeouts)
        if "Restart-Computer" in script
    ]
    assert reboot_timeouts == [cdj._REBOOT_TIMEOUT_SECONDS]


# --------------------------------------------------------------------------- #
# Autopilot hardware-hash capture
# --------------------------------------------------------------------------- #
# A real MDM_DevDetail_Ext01 blob is thousands of base64 characters.
_REAL_HASH = "T0RhdGFoYXNoQmxvYg" * 250


def test_parse_hash_output():
    assert cdj.parse_hash_output(
        {"out-data": "SERIAL=ABC123\r\nHASH=T0RhdGFoYXNo\r\n"}) == ("ABC123", "T0RhdGFoYXNo")
    assert cdj.parse_hash_output({"out-data": ""}) == ("", "")
    assert cdj.parse_hash_output({}) == ("", "")


def test_parse_hash_output_reassembles_a_wrapped_hash():
    """PowerShell's formatter hard-wraps long strings at the host width. The
    capture script writes through [Console]::Out so this should not happen, but
    base64 carries no whitespace, so reassembly is lossless insurance against a
    host that wraps anyway - versus silently keeping only the first line."""
    wrapped = "\r\n".join(_REAL_HASH[i:i + 120] for i in range(0, len(_REAL_HASH), 120))
    serial, hardware_hash = cdj.parse_hash_output(
        {"out-data": f"SERIAL=SN-1\r\nHASH={wrapped}\r\n"})
    assert serial == "SN-1"
    assert hardware_hash == _REAL_HASH


def test_guest_scripts_write_through_console_out():
    """Write-Output routes through PowerShell's formatter, which wraps at the
    host width - and would truncate the hash blob to its first line."""
    assert "[Console]::Out.WriteLine('HASH=' + $h)" in cdj._HASH_CAPTURE_PS
    assert "Write-Output" not in cdj._HASH_CAPTURE_PS
    assert "Write-Output" not in cdj._PROBE_PS


def _hash_guest(out):
    def guest(node, vmid, script, timeout=None):
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
        pg_conn, guest_exec=_hash_guest(f"SERIAL=SN-1\r\nHASH={_REAL_HASH}"),
        persist_hash=persist_hash, append_event=cloudosd_pg.append_event)

    assert summary["captured"] == 1
    assert _hash_step_state(pg_conn, rid) == "done"
    assert captured[0]["serial"] == "SN-1"
    assert captured[0]["hardware_hash"] == _REAL_HASH
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


def test_hash_capture_rejects_a_truncated_hash(pg_conn):
    """A short blob means the read was cut off. Persisting it would write an
    Autopilot CSV Intune rejects, with the step marked done and no signal."""
    from web import cloudosd_pg
    _init_db(pg_conn)
    rid = _make_run(pg_conn, domain_join=_DC_DOMAIN_JOIN)

    def persist_hash(**kw):  # pragma: no cover - must not run on a short hash
        raise AssertionError("should not persist a truncated hash")

    summary = cdj.run_pending_hash_captures(
        pg_conn, guest_exec=_hash_guest(f"SERIAL=SN-1\r\nHASH={_REAL_HASH[:120]}"),
        persist_hash=persist_hash, append_event=cloudosd_pg.append_event)

    assert summary["failed"] == 1
    assert "truncated capture" in summary["results"][0]["error"]
    assert _hash_step_state(pg_conn, rid) == "pending"
    events = {e["event_type"] for e in cloudosd_pg.list_events(pg_conn, rid)}
    assert "autopilot_hash_capture_failed" in events


def test_hash_capture_skips_stale_run(pg_conn):
    _init_db(pg_conn)
    rid = _make_run(pg_conn, domain_join=_DC_DOMAIN_JOIN)
    called = []

    def guest(*a, **kw):
        called.append(a)
        return {}

    summary = cdj.run_pending_hash_captures(
        pg_conn, guest_exec=guest, persist_hash=lambda **k: None,
        max_heartbeat_age_seconds=1800)

    assert summary["stale"] == 1
    assert called == []
    assert _hash_step_state(pg_conn, rid) == "pending"


def test_hash_capture_skips_a_run_the_join_pass_just_rebooted(pg_conn):
    """Probing a guest we forced down moments ago only burns the guest_exec
    timeout to learn it is unreachable."""
    _init_db(pg_conn)
    rid = _make_run(pg_conn, domain_join=_DC_DOMAIN_JOIN)
    called = []

    def guest(*a, **kw):  # pragma: no cover - must not be probed
        called.append(a)
        return {}

    summary = cdj.run_pending_hash_captures(
        pg_conn, guest_exec=guest, persist_hash=lambda **k: None,
        skip_run_ids={rid})

    assert summary["rebooting"] == 1
    assert called == []
    assert _hash_step_state(pg_conn, rid) == "pending"


def test_join_pass_reports_rebooted_runs_for_the_hash_pass(pg_conn):
    _init_db(pg_conn)
    rid = _make_run(pg_conn, domain_join=_DC_DOMAIN_JOIN)

    summary = cdj.run_pending_joins(
        pg_conn, guest_exec=_FakeGuest(), resolve_credential=_cred)

    assert summary["rebooted_run_ids"] == [rid]


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

    assert result == {"waiting": 1, "advanced": 1, "stale": 0}
    assert seen == [rid]


def test_advance_skips_a_stale_wedged_run(pg_conn):
    """The advancer carries the same stale-run guard as the two probing passes,
    so a permanently wedged run is not re-driven every tick forever."""
    _init_db(pg_conn)
    rid = _make_run(pg_conn, domain_join=_DC_DOMAIN_JOIN)
    pg_conn.execute(
        "UPDATE cloudosd_runs SET state = 'full_os_waiting_domain_join' WHERE run_id = %s",
        (rid,),
    )
    pg_conn.commit()
    _add_heartbeat(pg_conn, rid, age_seconds=7200)

    def mark_complete(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("a stale run must not be re-driven")

    result = cdj.advance_domain_joined_runs(
        pg_conn, latest_heartbeat=lambda r: {"received_at": "now"},
        mark_complete=mark_complete, max_heartbeat_age_seconds=1800)

    assert result == {"waiting": 1, "advanced": 0, "stale": 1}


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
