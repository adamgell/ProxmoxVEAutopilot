from pathlib import Path

from fastapi.testclient import TestClient


def test_delete_files_removes_listed_msi_files(tmp_path: Path, monkeypatch):
    from web import app as app_module

    monkeypatch.setattr(app_module, "FILE_SHELF_DIR", tmp_path)
    (tmp_path / "tool-one.msi").write_bytes(b"one")
    (tmp_path / "tool-two.msi").write_bytes(b"two")
    (tmp_path / "keep.msi").write_bytes(b"keep")

    client = TestClient(app_module.app)
    response = client.post(
        "/api/files/delete",
        data={"files": ["tool-one.msi", "tool-two.msi"]},
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {"ok": True, "deleted": 2}
    assert not (tmp_path / "tool-one.msi").exists()
    assert not (tmp_path / "tool-two.msi").exists()
    assert (tmp_path / "keep.msi").exists()


def test_delete_files_rejects_path_traversal(tmp_path: Path, monkeypatch):
    from web import app as app_module

    monkeypatch.setattr(app_module, "FILE_SHELF_DIR", tmp_path)
    sibling = tmp_path.parent / "outside.msi"
    sibling.write_bytes(b"do-not-touch")

    client = TestClient(app_module.app)
    response = client.post(
        "/api/files/delete",
        data={"files": ["../outside.msi"]},
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "deleted": 0}
    assert sibling.exists()
    sibling.unlink()


def test_delete_files_skips_non_msi_extensions(tmp_path: Path, monkeypatch):
    from web import app as app_module

    monkeypatch.setattr(app_module, "FILE_SHELF_DIR", tmp_path)
    (tmp_path / "notes.txt").write_text("keep me", encoding="utf-8")
    (tmp_path / "tool.msi").write_bytes(b"delete me")

    client = TestClient(app_module.app)
    response = client.post(
        "/api/files/delete",
        data={"files": ["notes.txt", "tool.msi"]},
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "deleted": 1}
    assert (tmp_path / "notes.txt").exists()
    assert not (tmp_path / "tool.msi").exists()


def test_delete_files_missing_files_count_as_zero(tmp_path: Path, monkeypatch):
    from web import app as app_module

    monkeypatch.setattr(app_module, "FILE_SHELF_DIR", tmp_path)
    client = TestClient(app_module.app)
    response = client.post(
        "/api/files/delete",
        data={"files": ["ghost.msi"]},
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "deleted": 0}


def test_delete_files_html_redirects_to_react_files(tmp_path: Path, monkeypatch):
    from web import app as app_module

    monkeypatch.setattr(app_module, "FILE_SHELF_DIR", tmp_path)
    (tmp_path / "tool.msi").write_bytes(b"x")

    client = TestClient(app_module.app)
    response = client.post(
        "/api/files/delete",
        data={"files": ["tool.msi"]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/react/files"
    assert not (tmp_path / "tool.msi").exists()
