import hashlib

import pytest

from web.artifact_sidecar import ArtifactKind, Sidecar
from web.artifact_store import ArtifactStore
from web.winpe_manifest_renderer import render_manifest, RendererError
from web.winpe_targets_db import WinpeTarget


def _seed_install_wim(store: ArtifactStore, tmp_path) -> str:
    """Register a fake install.wim into the store, return its sha."""
    src = tmp_path / "install.wim"
    content = b"fake install.wim content"
    src.write_bytes(content)
    sha = hashlib.sha256(content).hexdigest()
    store.register(
        src,
        Sidecar(kind=ArtifactKind.INSTALL_WIM, sha256=sha, size=len(content), metadata={}),
        extension="wim",
    )
    return sha


def _make_target(install_wim_sha: str, **overrides) -> WinpeTarget:
    return WinpeTarget(
        vm_uuid=overrides.get("vm_uuid", "11111111-2222-3333-4444-555555555555"),
        install_wim_sha=install_wim_sha,
        template_id=overrides.get("template_id", "win11-arm64-baseline"),
        params=overrides.get("params", {"computer_name": "AUTOPILOT-X1"}),
        created_at="2026-04-25T00:00:00Z",
        last_manifest_at=None,
    )


def test_renders_minimal_manifest(tmp_path):
    store = ArtifactStore(tmp_path)
    install_sha = _seed_install_wim(store, tmp_path)
    target = _make_target(install_sha)

    manifest = render_manifest(target, store)

    assert manifest["version"] == 1
    assert manifest["vmUuid"] == target.vm_uuid
    assert manifest["onError"] == "halt"
    step_types = [s["type"] for s in manifest["steps"]]
    assert step_types == [
        "partition", "apply-wim", "write-unattend",
        "set-registry", "bcdboot", "reboot",
    ]


def test_apply_wim_step_references_target_install_wim_sha(tmp_path):
    store = ArtifactStore(tmp_path)
    install_sha = _seed_install_wim(store, tmp_path)
    target = _make_target(install_sha)

    manifest = render_manifest(target, store)
    apply = next(s for s in manifest["steps"] if s["type"] == "apply-wim")
    assert apply["content"]["sha256"] == install_sha
    assert apply["content"]["size"] > 0


def test_unattend_step_caches_rendered_xml(tmp_path):
    """write-unattend points at a sha that, when fetched from the store, is the rendered XML."""
    store = ArtifactStore(tmp_path)
    install_sha = _seed_install_wim(store, tmp_path)
    target = _make_target(install_sha, params={"computer_name": "MY-COMPUTER-42"})

    manifest = render_manifest(target, store)
    unattend = next(s for s in manifest["steps"] if s["type"] == "write-unattend")
    sha = unattend["content"]["sha256"]
    record = store.lookup(sha)
    assert record is not None
    assert record.kind is ArtifactKind.UNATTEND_XML
    rendered = (store.root / record.relative_path).read_bytes().decode("utf-8")
    assert "MY-COMPUTER-42" in rendered


def test_unattend_caching_is_deterministic(tmp_path):
    """Identical params → identical rendered XML → identical sha (one cache row, not two)."""
    store = ArtifactStore(tmp_path)
    install_sha = _seed_install_wim(store, tmp_path)
    target = _make_target(install_sha)
    m1 = render_manifest(target, store)
    m2 = render_manifest(target, store)
    sha1 = next(s["content"]["sha256"] for s in m1["steps"] if s["type"] == "write-unattend")
    sha2 = next(s["content"]["sha256"] for s in m2["steps"] if s["type"] == "write-unattend")
    assert sha1 == sha2
    # Only 2 rows: install.wim + unattend.xml.
    assert len(store.list_artifacts()) == 2


def test_unknown_install_wim_raises(tmp_path):
    """Target references an install.wim sha that isn't in the store — renderer fails fast."""
    store = ArtifactStore(tmp_path)
    target = _make_target("0" * 64)  # bogus sha
    with pytest.raises(RendererError, match="install.wim"):
        render_manifest(target, store)


def test_set_registry_step_carries_computer_name(tmp_path):
    """The minimal manifest writes ComputerName via offline registry too (belt-and-braces with the unattend)."""
    store = ArtifactStore(tmp_path)
    install_sha = _seed_install_wim(store, tmp_path)
    target = _make_target(install_sha, params={"computer_name": "FOO-BAR"})

    manifest = render_manifest(target, store)
    reg = next(s for s in manifest["steps"] if s["type"] == "set-registry")
    cn_entry = next(k for k in reg["keys"] if k["name"] == "ComputerName")
    assert cn_entry["value"] == "FOO-BAR"
