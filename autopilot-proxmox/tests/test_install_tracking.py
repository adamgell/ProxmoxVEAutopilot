from fastapi.testclient import TestClient


def _client(pg_conn, pg_dsn, monkeypatch):
    from web import install_tracking_pg

    install_tracking_pg.reset_for_tests(pg_conn)
    monkeypatch.setenv("AUTOPILOT_DATABASE_URL", pg_dsn)
    from web.app import app

    return TestClient(app)


def test_install_tracking_api_seeds_pvetest_checklist(pg_conn, pg_dsn, monkeypatch):
    client = _client(pg_conn, pg_dsn, monkeypatch)

    response = client.get("/api/install-tracking")

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == 1
    assert payload["run"]["run_id"] == "pvetest-clean-install"
    ids = {item["item_id"] for item in payload["items"]}
    assert "pve-foundation" in ids
    assert "osdeploy-e2e-run" in ids
    assert "osdcloud-catalog" in ids
    assert "osdeploy-source-media" in ids
    assert "agent-seed-artifacts" in ids
    assert payload["summary"]["total"] >= 14
    osdeploy = next(item for item in payload["items"] if item["item_id"] == "osdeploy-e2e-run")
    assert osdeploy["target"] == "VMID 106"
    assert osdeploy["evidence"]["run_id"] == "d6376517-2306-49ea-bfbe-228ed6cb499a"


def test_install_tracking_runs_can_be_created_and_are_isolated(pg_conn, pg_dsn, monkeypatch):
    client = _client(pg_conn, pg_dsn, monkeypatch)

    created = client.post(
        "/api/install-tracking/runs",
        json={"name": "Fresh pvetest reinstall", "target": "pvetest-2", "source": "operator"},
    )

    assert created.status_code == 200
    run_id = created.json()["run"]["run_id"]
    assert run_id != "pvetest-clean-install"

    update = client.post(
        f"/api/install-tracking/runs/{run_id}/items/windows-build-box",
        json={
            "status": "blocked",
            "detail": "waiting for ISO seed",
            "source": "manual",
            "evidence": {"artifact": "missing"},
        },
    )

    assert update.status_code == 200
    assert update.json()["item"]["status"] == "blocked"

    new_run = client.get(f"/api/install-tracking/runs/{run_id}").json()
    default_run = client.get("/api/install-tracking/runs/pvetest-clean-install").json()
    new_item = next(item for item in new_run["items"] if item["item_id"] == "windows-build-box")
    default_item = next(item for item in default_run["items"] if item["item_id"] == "windows-build-box")
    assert new_item["status"] == "blocked"
    assert default_item["status"] == "pending"
    assert new_run["summary"]["blockers"] == 1
    assert new_run["events"][0]["run_id"] == run_id


def test_install_tracking_init_migrates_existing_global_rows(pg_conn, pg_dsn, monkeypatch):
    from web import install_tracking_pg

    install_tracking_pg.reset_for_tests(pg_conn)
    pg_conn.execute(
        """
        CREATE TABLE install_tracking_items (
            item_id text PRIMARY KEY,
            category text NOT NULL,
            label text NOT NULL,
            description text NOT NULL DEFAULT '',
            target text NOT NULL DEFAULT '',
            status text NOT NULL,
            detail text NOT NULL DEFAULT '',
            evidence_json jsonb NOT NULL DEFAULT '{}'::jsonb,
            source text NOT NULL DEFAULT '',
            sort_order integer NOT NULL DEFAULT 1000,
            created_at timestamptz NOT NULL,
            updated_at timestamptz NOT NULL
        )
        """
    )
    pg_conn.execute(
        """
        CREATE TABLE install_tracking_events (
            event_id bigserial PRIMARY KEY,
            item_id text NOT NULL REFERENCES install_tracking_items(item_id) ON DELETE CASCADE,
            status text NOT NULL,
            detail text NOT NULL DEFAULT '',
            evidence_json jsonb NOT NULL DEFAULT '{}'::jsonb,
            source text NOT NULL DEFAULT '',
            created_at timestamptz NOT NULL
        )
        """
    )
    pg_conn.execute(
        """
        INSERT INTO install_tracking_items (
            item_id, category, label, status, detail, created_at, updated_at
        )
        VALUES ('legacy-gate', 'Legacy', 'Legacy gate', 'running', 'keep me', now(), now())
        """
    )
    pg_conn.execute(
        """
        INSERT INTO install_tracking_events (item_id, status, detail, created_at)
        VALUES ('legacy-gate', 'running', 'legacy event', now())
        """
    )
    pg_conn.commit()

    install_tracking_pg.init(pg_conn)

    migrated = install_tracking_pg.get_item(pg_conn, "pvetest-clean-install", "legacy-gate")
    events = install_tracking_pg.list_events(pg_conn, "pvetest-clean-install")
    assert migrated["detail"] == "keep me"
    assert migrated["run_id"] == "pvetest-clean-install"
    assert events[0]["run_id"] == "pvetest-clean-install"


def test_install_tracking_runs_list_latest_active_first(pg_conn, pg_dsn, monkeypatch):
    client = _client(pg_conn, pg_dsn, monkeypatch)

    client.post("/api/install-tracking/runs", json={"name": "Run A", "target": "pvetest-a"})
    client.post("/api/install-tracking/runs", json={"name": "Run B", "target": "pvetest-b"})

    response = client.get("/api/install-tracking/runs")

    assert response.status_code == 200
    runs = response.json()["runs"]
    assert runs[0]["name"] == "Run B"
    assert {run["run_id"] for run in runs} >= {"pvetest-clean-install"}


def test_install_tracking_refresh_evidence_updates_pending_gates(pg_conn, pg_dsn, monkeypatch):
    from web import install_tracking_pg

    install_tracking_pg.reset_for_tests(pg_conn)
    install_tracking_pg.init(pg_conn)
    run = install_tracking_pg.default_run(pg_conn)

    item = install_tracking_pg.refresh_evidence(
        pg_conn,
        run["run_id"],
        {
            "controller_stack": {"healthy": True, "containers": {"autopilot": "healthy"}},
            "mcp_docs": {"tool_count": 108, "doc_count": 66},
        },
    )

    assert item["summary"]["complete"] >= 2
    controller = install_tracking_pg.get_item(pg_conn, run["run_id"], "controller-stack")
    assert controller["status"] == "ready"
    assert controller["evidence"]["containers"]["autopilot"] == "healthy"
    assert install_tracking_pg.get_item(pg_conn, run["run_id"], "osdeploy-e2e-run")["status"] == "ready"


def test_install_tracking_update_records_event_and_redacts_secrets(pg_conn, pg_dsn, monkeypatch):
    client = _client(pg_conn, pg_dsn, monkeypatch)

    response = client.post(
        "/api/install-tracking/items/windows-build-box",
        json={
            "status": "running",
            "detail": "seeding artifacts from build box",
            "source": "pvetest",
            "evidence": {"password": "do-not-store", "artifact": "winpe.iso"},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["item"]["status"] == "running"
    assert body["item"]["evidence"]["password"] == "[redacted]"
    assert body["item"]["evidence"]["artifact"] == "winpe.iso"

    listing = client.get("/api/install-tracking").json()
    event = listing["events"][0]
    assert event["item_id"] == "windows-build-box"
    assert event["status"] == "running"
    assert event["evidence"]["password"] == "[redacted]"


def test_install_tracking_page_renders_nav_and_table(pg_conn, pg_dsn, monkeypatch):
    client = _client(pg_conn, pg_dsn, monkeypatch)

    response = client.get("/install-tracking")

    assert response.status_code == 200
    assert "Deployment Readiness" in response.text
    assert "Deploy surfaces covered" in response.text
    assert "OSDCloud catalog and deploy options ready" in response.text
    assert "OSDeploy source media and cache ready" in response.text
    assert "pvetest-clean-install" in response.text
    assert "Clean OSDeploy run completes" in response.text
    assert 'href="/install-tracking"' in response.text
    assert "Create Install Run" not in response.text
    assert "data-install-update" not in response.text
    assert "Refresh Evidence" not in response.text
