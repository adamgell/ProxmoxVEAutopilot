"""Integration tests that run against a live autopilot web UI.

Default base URL: http://192.168.2.4:5000 (the autopilot-docker LXC).
Override with AUTOPILOT_BASE_URL=http://host:port/.

Run with:
    .venv/bin/python -m pytest tests/integration -v --run-integration

These tests intentionally exercise only **read-only** endpoints and
**self-cleaning** CRUD paths (resources named ``claude-harness-*`` that
are deleted in fixture teardown). They must not trigger a provision,
never create an actual VM, and never mutate seeded data.
"""
from __future__ import annotations

import os
import time
import uuid

import pytest
import requests


pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def base_url() -> str:
    return os.environ.get("AUTOPILOT_BASE_URL", "http://192.168.2.4:5000").rstrip("/")


@pytest.fixture(scope="module")
def session(base_url) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "claude-test-harness/1.0"})
    # Verify the host is up before starting the suite; fail fast with a
    # useful message instead of letting every test error independently.
    try:
        r = s.get(base_url + "/", timeout=5)
        r.raise_for_status()
    except requests.RequestException as e:
        pytest.skip(f"autopilot host not reachable at {base_url}: {e}")
    return s


@pytest.fixture
def test_tag() -> str:
    """A short unique suffix so parallel runs / leftover rows don't collide."""
    return uuid.uuid4().hex[:8]


# ------------------------------------------------------------------
# Health / baseline
# ------------------------------------------------------------------

def test_home_page_renders(session, base_url):
    r = session.get(base_url + "/", timeout=10)
    assert r.status_code == 200
    assert "Proxmox VE Autopilot" in r.text


def test_version_endpoint(session, base_url):
    """Version endpoint returns the running image's git sha + build time."""
    r = session.get(base_url + "/api/version", timeout=10)
    assert r.status_code == 200
    body = r.json()
    running = body.get("running", {})
    assert "sha" in running
    assert "build_time" in running
    # Sanity-check the sha looks like a commit hash.
    assert len(running["sha"]) >= 7
    assert running["sha"].isalnum()


# ------------------------------------------------------------------
# Seeded content (the default sequences + credential)
# ------------------------------------------------------------------

def test_seeded_sequences_present(session, base_url):
    r = session.get(base_url + "/api/sequences", timeout=10)
    assert r.status_code == 200
    names = {s["name"] for s in r.json()}
    assert "Entra Join (default)" in names
    assert "AD Domain Join — Local Admin" in names
    assert "Hybrid Autopilot (stub)" in names


def test_entra_join_is_default(session, base_url):
    r = session.get(base_url + "/api/sequences", timeout=10)
    by_name = {s["name"]: s for s in r.json()}
    assert by_name["Entra Join (default)"]["is_default"] is True
    assert by_name["Entra Join (default)"]["produces_autopilot_hash"] is True


def test_default_local_admin_credential_seeded(session, base_url):
    r = session.get(base_url + "/api/credentials?type=local_admin", timeout=10)
    assert r.status_code == 200
    names = {c["name"] for c in r.json()}
    assert "default-local-admin" in names


def test_credential_payload_never_in_list_response(session, base_url):
    """The list endpoint must omit encrypted_blob and payload — only the
    single-resource GET should decrypt. Guard against a future regression
    that accidentally serializes the blob."""
    r = session.get(base_url + "/api/credentials", timeout=10)
    for cred in r.json():
        assert "payload" not in cred
        assert "encrypted_blob" not in cred


# ------------------------------------------------------------------
# UI pages render
# ------------------------------------------------------------------

def test_sequences_page_renders(session, base_url):
    r = session.get(base_url + "/sequences", timeout=10)
    assert r.status_code == 200
    assert "Entra Join (default)" in r.text


def test_credentials_page_renders(session, base_url):
    r = session.get(base_url + "/credentials", timeout=10)
    assert r.status_code == 200
    assert "default-local-admin" in r.text


def test_provision_page_has_sequence_dropdown(session, base_url):
    """After Phase B.1 the provision form has a <select name='sequence_id'>
    that lists all sequences. This is the smoke check that the wiring works."""
    r = session.get(base_url + "/provision", timeout=10)
    assert r.status_code == 200
    assert 'name="sequence_id"' in r.text
    # The default should be pre-selected.
    assert "Entra Join (default)" in r.text


# ------------------------------------------------------------------
# Round-trip CRUD (self-cleaning — no side effects)
# ------------------------------------------------------------------

def _delete_credentials_by_prefix(session, base_url, prefix):
    """Best-effort cleanup — called by fixture teardown."""
    r = session.get(base_url + "/api/credentials", timeout=10)
    if r.status_code != 200:
        return
    for cred in r.json():
        if cred["name"].startswith(prefix):
            session.delete(base_url + f"/api/credentials/{cred['id']}", timeout=10)


def _delete_sequences_by_prefix(session, base_url, prefix):
    r = session.get(base_url + "/api/sequences", timeout=10)
    if r.status_code != 200:
        return
    for seq in r.json():
        if seq["name"].startswith(prefix):
            session.delete(base_url + f"/api/sequences/{seq['id']}", timeout=10)


@pytest.fixture
def cleanup_test_resources(session, base_url):
    """Ensure no leftover `claude-harness-*` rows at setup or teardown."""
    _delete_sequences_by_prefix(session, base_url, "claude-harness-")
    _delete_credentials_by_prefix(session, base_url, "claude-harness-")
    yield
    _delete_sequences_by_prefix(session, base_url, "claude-harness-")
    _delete_credentials_by_prefix(session, base_url, "claude-harness-")


def test_credential_create_get_delete(session, base_url, test_tag, cleanup_test_resources):
    name = f"claude-harness-cred-{test_tag}"
    r = session.post(
        base_url + "/api/credentials",
        json={"name": name, "type": "local_admin",
              "payload": {"username": "harness", "password": "harness-pw"}},
        timeout=10,
    )
    assert r.status_code == 201, r.text
    cid = r.json()["id"]

    # Appears in list
    r = session.get(base_url + "/api/credentials?type=local_admin", timeout=10)
    assert any(c["name"] == name for c in r.json())

    # Full get decrypts the payload
    r = session.get(base_url + f"/api/credentials/{cid}", timeout=10)
    assert r.status_code == 200
    assert r.json()["payload"]["password"] == "harness-pw"

    # Delete works + returns 404 afterward
    assert session.delete(base_url + f"/api/credentials/{cid}", timeout=10).status_code == 200
    assert session.get(base_url + f"/api/credentials/{cid}", timeout=10).status_code == 404


def test_sequence_create_with_steps(session, base_url, test_tag, cleanup_test_resources):
    name = f"claude-harness-seq-{test_tag}"
    r = session.post(
        base_url + "/api/sequences",
        json={
            "name": name,
            "description": "created by claude test harness",
            "is_default": False,
            "produces_autopilot_hash": True,
            "steps": [
                {"step_type": "set_oem_hardware",
                 "params": {"oem_profile": "dell-latitude-5540"}, "enabled": True},
                {"step_type": "autopilot_entra", "params": {}, "enabled": True},
            ],
        },
        timeout=10,
    )
    assert r.status_code == 201, r.text
    sid = r.json()["id"]

    # Read back, verify shape
    r = session.get(base_url + f"/api/sequences/{sid}", timeout=10)
    assert r.status_code == 200
    seq = r.json()
    assert seq["name"] == name
    assert seq["produces_autopilot_hash"] is True
    assert [s["step_type"] for s in seq["steps"]] == ["set_oem_hardware", "autopilot_entra"]

    # Delete works
    assert session.delete(base_url + f"/api/sequences/{sid}", timeout=10).status_code == 200


def test_delete_seeded_credential_blocked_by_reference(session, base_url):
    """The seeded default-local-admin is referenced by the seeded 'Entra
    Join (default)' sequence (step is disabled but still references the
    credential_id). Deletion must return 409 with the sequence ID.

    The B.1 seed has local_admin marked enabled=False but its params_json
    STILL carries credential_id — the in-use guard looks at params_json
    unconditionally, so the block is correct.
    """
    r = session.get(base_url + "/api/credentials?type=local_admin", timeout=10)
    cred = next((c for c in r.json() if c["name"] == "default-local-admin"), None)
    if cred is None:
        pytest.skip("seed missing; nothing to test")
    r = session.delete(base_url + f"/api/credentials/{cred['id']}", timeout=10)
    # Expect 409 IF any seeded sequence references it. If the seed was
    # changed to not reference it, this test becomes vacuous — guard
    # against that with a soft assertion.
    if r.status_code == 200:
        pytest.fail(
            "default-local-admin was deleted — no seeded sequence referenced it. "
            "Check seed data; this would break the no-regression guarantee."
        )
    assert r.status_code == 409
    assert r.json().get("sequence_ids") or "sequence" in r.text.lower()


# ------------------------------------------------------------------
# Compiler end-to-end check via the UI surface
# ------------------------------------------------------------------

def test_sequence_builder_page_loads_for_seeded_default(session, base_url):
    """Editing the seeded default sequence must render the builder page
    cleanly. This exercises the code path that loads oem_profiles +
    fetches credentials."""
    r = session.get(base_url + "/api/sequences", timeout=10)
    default = next(s for s in r.json() if s["is_default"])
    r = session.get(base_url + f"/sequences/{default['id']}/edit", timeout=10)
    assert r.status_code == 200
    # The builder template renders the seed's steps inline as JSON
    assert "set_oem_hardware" in r.text or "autopilot_entra" in r.text
