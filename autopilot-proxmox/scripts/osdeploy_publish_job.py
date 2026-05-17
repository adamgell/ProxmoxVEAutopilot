#!/usr/bin/env python3
"""Publish a built OSDeploy ISO artifact to Proxmox ISO storage."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _safe_iso_name(path: Path) -> str:
    name = path.name
    if not name.lower().endswith(".iso"):
        raise ValueError(f"OSDeploy publish source is not an ISO: {path}")
    if any(char in name for char in ("/", "\\", ":", "\x00")):
        raise ValueError(f"OSDeploy publish ISO name is not safe: {name}")
    return name


def upload_iso_to_proxmox(
    *,
    iso_path: Path,
    node: str,
    storage: str,
    iso_name: str,
    timeout: int = 900,
) -> str:
    from web import app as web_app

    web_app._proxmox_upload_file(
        f"/nodes/{node}/storage/{storage}/upload",
        iso_path,
        data={"content": "iso"},
        field_name="filename",
        content_type="application/x-iso9660-image",
    )
    return f"{storage}:iso/{iso_name}"


def publish_artifact(*, artifact_id: str, node: str, storage: str, job_id: str) -> dict:
    from web import db_pg, osdeploy_pg

    with db_pg.connection() as conn:
        osdeploy_pg.init(conn)
        artifact = osdeploy_pg.get_artifact(conn, artifact_id)
        if not artifact:
            raise RuntimeError(f"OSDeploy artifact not found: {artifact_id}")
        iso_path = Path(artifact["iso_path"])
        if not iso_path.is_file():
            raise RuntimeError(f"OSDeploy ISO not found: {iso_path}")
        iso_name = _safe_iso_name(iso_path)
        volid = upload_iso_to_proxmox(
            iso_path=iso_path,
            node=node,
            storage=storage,
            iso_name=iso_name,
        )
        updated = osdeploy_pg.update_artifact_proxmox_volid(
            conn,
            artifact_id=artifact_id,
            proxmox_volid=volid,
            publish_job_id=job_id,
        )
    return {"ok": True, "artifact": updated, "target_volid": volid}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish OSDeploy ISO artifact")
    parser.add_argument("action", choices=["publish"])
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--storage", required=True)
    parser.add_argument("--job-id", default="manual-osdeploy-publish")
    args = parser.parse_args(argv)

    result = publish_artifact(
        artifact_id=args.artifact_id,
        node=args.node,
        storage=args.storage,
        job_id=args.job_id,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
