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


# ---------------------------------------------------------------------------
# Microservice split integration tests (Tasks 22-25)
# Run with: pytest tests/integration/test_live.py --run-integration -v
# Prereqs:
#   - Target box is running the split compose stack
#   - AUTOPILOT_ENABLE_TEST_JOBS=1 is set in web container environment
#   - --scale autopilot-builder=N for scale test
# These tests adapt to the existing (session, base_url) fixtures above:
#   - `session` is a requests.Session pre-flighted against the live box
#   - `live_host` is derived from AUTOPILOT_LIVE_HOST (default 192.168.2.4)
#     for the ssh-based tests (singleton + wedge).
# ---------------------------------------------------------------------------

import subprocess
from urllib.parse import urlparse


@pytest.fixture(scope="module")
def live_host() -> str:
    """Hostname of the live autopilot box used for ssh-driven checks
    (singleton + wedge tests). Falls back to the host part of
    AUTOPILOT_BASE_URL if AUTOPILOT_LIVE_HOST isn't set."""
    explicit = os.environ.get("AUTOPILOT_LIVE_HOST")
    if explicit:
        return explicit
    base = os.environ.get("AUTOPILOT_BASE_URL", "http://192.168.2.4:5000")
    parsed = urlparse(base)
    return parsed.hostname or "192.168.2.4"


def test_kill_stops_running_job_within_10s(session, base_url):
    """Enqueue a long-sleeping test job, POST /kill, observe status
    transition within 10 seconds (5s heartbeat + grace)."""
    r = session.post(base_url + "/api/jobs/test-long-sleep",
                     data={"duration": "60"}, timeout=10)
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]

    # Wait for builder to claim (up to 10s).
    deadline = time.time() + 10
    job = None
    while time.time() < deadline:
        job = session.get(base_url + f"/api/jobs/{job_id}", timeout=10).json()
        if job.get("status") == "running":
            break
        time.sleep(0.5)
    assert job and job.get("status") == "running", f"expected running, got {job}"

    session.post(base_url + f"/api/jobs/{job_id}/kill", timeout=10,
                 allow_redirects=False)

    # Expect terminal status within 10s (5s max heartbeat cycle + grace).
    deadline = time.time() + 10
    while time.time() < deadline:
        job = session.get(base_url + f"/api/jobs/{job_id}", timeout=10).json()
        if job.get("status") in ("complete", "failed"):
            break
        time.sleep(0.5)
    assert job.get("status") in ("complete", "failed"), \
        f"kill didn't terminate within 10s, status={job.get('status')}"


def test_scale_three_builders_runs_three_concurrent_jobs(session, base_url):
    """With --scale autopilot-builder=3, peak concurrency is 3.
    Requires operator to have scaled the compose stack before running."""
    ids = []
    for _ in range(5):
        r = session.post(base_url + "/api/jobs/test-long-sleep",
                         data={"duration": "30"}, timeout=10)
        ids.append(r.json()["id"])

    deadline = time.time() + 15
    peak_running = 0
    try:
        while time.time() < deadline:
            rows = session.get(base_url + "/api/jobs?limit=10", timeout=10).json()
            running = sum(
                1 for row in rows
                if row.get("id") in ids and row.get("status") == "running"
            )
            peak_running = max(peak_running, running)
            if peak_running >= 3:
                break
            time.sleep(0.5)
        assert peak_running == 3, \
            f"expected 3 concurrent builders, peak={peak_running}"
    finally:
        # Best-effort cleanup so the 30s sleeps don't linger for the
        # next test (especially when run repeatedly against a live box).
        for job_id in ids:
            try:
                session.post(base_url + f"/api/jobs/{job_id}/kill",
                             timeout=5, allow_redirects=False)
            except Exception:
                pass


def test_monitor_singleton_rejects_second_instance(live_host):
    """A second monitor container sharing the volume should exit 0 with
    the 'already running elsewhere' warning (flock holds the lock)."""
    result = subprocess.run(
        [
            "ssh", f"root@{live_host}",
            "docker run --rm "
            "-v /opt/ProxmoxVEAutopilot/autopilot-proxmox/output:/app/output "
            "ghcr.io/adamgell/proxmox-autopilot:latest monitor 2>&1 | head -5",
        ],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, \
        f"second monitor crashed instead of exiting cleanly: {result.stderr}"
    assert "already running elsewhere" in result.stdout


def test_web_responsive_when_builder_stalls(session, base_url, live_host):
    """SIGSTOP the builder's ansible subprocess. Web routes must stay
    under 1s each — the whole point of the split."""
    r = session.post(base_url + "/api/jobs/test-long-sleep",
                     data={"duration": "120"}, timeout=10)
    job_id = r.json()["id"]

    # Wait for it to start running.
    deadline = time.time() + 10
    while time.time() < deadline:
        status = session.get(base_url + f"/api/jobs/{job_id}",
                             timeout=10).json().get("status")
        if status == "running":
            break
        time.sleep(0.5)

    # SIGSTOP the ansible subprocess to simulate a wedge.
    subprocess.run(
        ["ssh", f"root@{live_host}",
         "pkill -STOP -f 'ansible-playbook.*_test_long_sleep'"],
        check=True, timeout=10,
    )

    try:
        for path in ("/healthz", "/vms", "/monitoring", "/jobs"):
            t0 = time.time()
            r = session.get(base_url + path, timeout=5)
            elapsed = time.time() - t0
            assert r.status_code == 200, f"{path} returned {r.status_code}"
            assert elapsed < 1.0, f"{path} took {elapsed:.2f}s (>1s budget)"
    finally:
        # SIGCONT so the process can finish cleanly, then kill.
        subprocess.run(
            ["ssh", f"root@{live_host}",
             "pkill -CONT -f 'ansible-playbook.*_test_long_sleep'"],
            timeout=10,
        )
        session.post(base_url + f"/api/jobs/{job_id}/kill",
                     timeout=5, allow_redirects=False)
