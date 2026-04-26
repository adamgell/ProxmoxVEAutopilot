from web.winpe_checkin_db import (
    Checkin,
    WinpeCheckinDb,
)


def _db(tmp_path):
    return WinpeCheckinDb(tmp_path / "checkins.db")


def test_init_creates_table(tmp_path):
    db = _db(tmp_path)
    assert db.list_for_vm("u1") == []


def test_record_and_list(tmp_path):
    db = _db(tmp_path)
    db.record(Checkin(
        vm_uuid="u1",
        step_id="partition",
        status="ok",
        timestamp="2026-04-25T22:00:00Z",
        duration_sec=4.2,
        log_tail="formatted disk 0; created GPT layout\n",
        error_message=None,
        extra={"esp": "S:", "windows": "W:"},
    ))
    db.record(Checkin(
        vm_uuid="u1",
        step_id="apply-wim",
        status="ok",
        timestamp="2026-04-25T22:01:30Z",
        duration_sec=84.1,
        log_tail="applied install.wim to W:\\\n",
        error_message=None,
        extra={},
    ))
    rows = db.list_for_vm("u1")
    assert len(rows) == 2
    assert rows[0].step_id == "partition"
    assert rows[1].step_id == "apply-wim"
    assert rows[1].duration_sec == 84.1


def test_list_filters_by_vm_uuid(tmp_path):
    db = _db(tmp_path)
    db.record(Checkin(vm_uuid="u1", step_id="p", status="ok",
                      timestamp="2026-04-25T22:00:00Z", duration_sec=1.0,
                      log_tail="", error_message=None, extra={}))
    db.record(Checkin(vm_uuid="u2", step_id="p", status="ok",
                      timestamp="2026-04-25T22:00:00Z", duration_sec=1.0,
                      log_tail="", error_message=None, extra={}))
    assert len(db.list_for_vm("u1")) == 1
    assert len(db.list_for_vm("u2")) == 1
    assert len(db.list_for_vm("u3")) == 0


def test_record_idempotent_on_uuid_step_timestamp(tmp_path):
    """Same (vm_uuid, step_id, timestamp) tuple — duplicate POSTs from a retrying PE
    must not insert duplicate rows. We use INSERT OR REPLACE keyed on the triple."""
    db = _db(tmp_path)
    c = Checkin(vm_uuid="u1", step_id="p", status="starting",
                timestamp="2026-04-25T22:00:00Z", duration_sec=0.0,
                log_tail="", error_message=None, extra={})
    db.record(c)
    db.record(c)  # duplicate — same triple
    assert len(db.list_for_vm("u1")) == 1


def test_status_update_on_same_triple(tmp_path):
    """If PE first records 'starting' then later records 'ok' with the same timestamp
    (this is unusual; normally the timestamp would advance), we accept the latter as
    an idempotent rewrite. In practice timestamps will differ and we'll have two rows."""
    db = _db(tmp_path)
    db.record(Checkin(vm_uuid="u1", step_id="p", status="starting",
                      timestamp="2026-04-25T22:00:00Z", duration_sec=0.0,
                      log_tail="", error_message=None, extra={}))
    db.record(Checkin(vm_uuid="u1", step_id="p", status="ok",
                      timestamp="2026-04-25T22:00:00Z", duration_sec=2.5,
                      log_tail="done", error_message=None, extra={}))
    rows = db.list_for_vm("u1")
    assert len(rows) == 1
    assert rows[0].status == "ok"
    assert rows[0].duration_sec == 2.5


def test_error_message_persists(tmp_path):
    db = _db(tmp_path)
    db.record(Checkin(vm_uuid="u1", step_id="apply", status="error",
                      timestamp="2026-04-25T22:00:00Z", duration_sec=1.0,
                      log_tail="...sha256 mismatch on fetched content",
                      error_message="sha256 mismatch: expected aaa, got bbb",
                      extra={}))
    row = db.list_for_vm("u1")[0]
    assert row.status == "error"
    assert row.error_message == "sha256 mismatch: expected aaa, got bbb"
