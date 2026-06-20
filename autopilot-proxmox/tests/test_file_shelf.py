from pathlib import Path

from fastapi.testclient import TestClient


def test_files_page_lists_uploaded_files(tmp_path: Path, monkeypatch):
    from web import app as app_module

    monkeypatch.setattr(app_module, "FILE_SHELF_DIR", tmp_path)
    (tmp_path / "agent-tools.msi").write_bytes(b"msi-one")
    (tmp_path / "notes.txt").write_text("plain text", encoding="utf-8")

    client = TestClient(app_module.app)
    response = client.get("/legacy/files", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/react/files"

    response = client.get("/api/files")
    assert response.status_code == 200
    files = response.json()["files"]
    assert [row["name"] for row in files] == ["agent-tools.msi", "notes.txt"]
    assert files[0]["size_bytes"] == 7
    assert files[0]["url"] == "/files/agent-tools.msi"
    assert files[1]["size_bytes"] == 10
    assert files[1]["url"] == "/files/notes.txt"


def test_upload_files_accepts_any_file_type_and_sanitizes_names(tmp_path: Path, monkeypatch):
    from web import app as app_module

    monkeypatch.setattr(app_module, "FILE_SHELF_DIR", tmp_path)

    client = TestClient(app_module.app)
    response = client.post(
        "/api/files/upload",
        files=[
            ("files", ("Agent Tools 1.0.msi", b"msi-bytes", "application/octet-stream")),
            ("files", ("ignore.exe", b"exe-bytes", "application/octet-stream")),
            ("files", ("../config.json", b"json-bytes", "application/json")),
        ],
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/react/files?uploaded=3"
    assert (tmp_path / "Agent_Tools_1.0.msi").read_bytes() == b"msi-bytes"
    assert (tmp_path / "ignore.exe").read_bytes() == b"exe-bytes"
    assert (tmp_path / "config.json").read_bytes() == b"json-bytes"


def test_upload_file_rejects_files_over_size_limit(tmp_path: Path, monkeypatch):
    from web import app as app_module

    monkeypatch.setattr(app_module, "FILE_SHELF_DIR", tmp_path)
    monkeypatch.setattr(app_module, "FILE_SHELF_MAX_BYTES", 4)

    client = TestClient(app_module.app)
    response = client.post(
        "/api/files/upload",
        files={"files": ("too-large.bin", b"12345", "application/octet-stream")},
        headers={"accept": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["error"] == "File too-large.bin exceeds 4 bytes"
    assert not (tmp_path / "too-large.bin").exists()


def test_download_file_serves_safe_file_paths(tmp_path: Path, monkeypatch):
    from web import app as app_module

    monkeypatch.setattr(app_module, "FILE_SHELF_DIR", tmp_path)
    (tmp_path / "tool.msi").write_bytes(b"download-me")
    (tmp_path / "not-msi.txt").write_bytes(b"download-text")

    client = TestClient(app_module.app)
    ok = client.get("/files/tool.msi")
    missing = client.get("/files/not-msi.txt")
    traversal = client.get("/files/..%2Fsecret.msi")

    assert ok.status_code == 200
    assert ok.content == b"download-me"
    assert ok.headers["content-type"].startswith("application/octet-stream")
    assert missing.status_code == 200
    assert missing.content == b"download-text"
    assert traversal.status_code in {403, 404}


def test_delete_files_removes_selected_files_for_react_callers(tmp_path: Path, monkeypatch):
    from web import app as app_module

    monkeypatch.setattr(app_module, "FILE_SHELF_DIR", tmp_path)
    (tmp_path / "tool.zip").write_bytes(b"delete-me")
    (tmp_path / "keep.msi").write_bytes(b"keep-me")

    client = TestClient(app_module.app)
    response = client.post(
        "/api/files/delete",
        data={"files": "tool.zip"},
        headers={"accept": "application/json"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "deleted": 1}
    assert not (tmp_path / "tool.zip").exists()
    assert (tmp_path / "keep.msi").exists()


def test_replace_file_keeps_existing_download_url_and_sanitizes_target(tmp_path: Path, monkeypatch):
    from web import app as app_module

    monkeypatch.setattr(app_module, "FILE_SHELF_DIR", tmp_path)
    (tmp_path / "tool.msi").write_bytes(b"old")

    client = TestClient(app_module.app)
    response = client.post(
        "/api/files/tool.msi/replace",
        files={"file": ("replacement.msi", b"new-msi", "application/octet-stream")},
        headers={"accept": "application/json"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "replaced": "tool.msi", "size_bytes": 7}
    assert (tmp_path / "tool.msi").read_bytes() == b"new-msi"
    assert not (tmp_path / "replacement.msi").exists()


def test_replace_file_accepts_any_replacement_upload(tmp_path: Path, monkeypatch):
    from web import app as app_module

    monkeypatch.setattr(app_module, "FILE_SHELF_DIR", tmp_path)
    (tmp_path / "tool.msi").write_bytes(b"old")

    client = TestClient(app_module.app)
    response = client.post(
        "/api/files/tool.msi/replace",
        files={"file": ("replacement.exe", b"exe", "application/octet-stream")},
        headers={"accept": "application/json"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "replaced": "tool.msi", "size_bytes": 3}
    assert (tmp_path / "tool.msi").read_bytes() == b"exe"


def test_replace_file_rejects_files_over_size_limit(tmp_path: Path, monkeypatch):
    from web import app as app_module

    monkeypatch.setattr(app_module, "FILE_SHELF_DIR", tmp_path)
    monkeypatch.setattr(app_module, "FILE_SHELF_MAX_BYTES", 4)
    (tmp_path / "tool.iso").write_bytes(b"old")

    client = TestClient(app_module.app)
    response = client.post(
        "/api/files/tool.iso/replace",
        files={"file": ("replacement.iso", b"12345", "application/octet-stream")},
        headers={"accept": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["error"] == "File replacement.iso exceeds 4 bytes"
    assert (tmp_path / "tool.iso").read_bytes() == b"old"


def test_files_download_routes_are_public_but_upload_api_is_not():
    from web import auth

    assert auth.is_exempt_path("/files")
    assert auth.is_exempt_path("/files/tool.msi")
    assert not auth.is_exempt_path("/api/files/upload")
    assert not auth.is_exempt_path("/api/files/delete")
    assert not auth.is_exempt_path("/api/files/tool.msi/replace")
