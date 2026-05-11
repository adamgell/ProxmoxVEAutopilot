#!/usr/bin/env python3
"""Upload CSVs produced by completed AutopilotAgent hash work items."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web import agent_telemetry_pg, db_pg  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--work-item",
        action="append",
        dest="work_items",
        required=True,
        help="Completed AutopilotAgent work item id. May be repeated.",
    )
    parser.add_argument("--hash-dir", required=True)
    parser.add_argument("--playbook", required=True)
    parser.add_argument("--group-tag", default="")
    return parser.parse_args()


def _result_filename(row: dict) -> str:
    result = row.get("result_json") or {}
    filename = str(result.get("filename") or "")
    if not filename:
        raise RuntimeError(f"{row['id']}: completed work item did not report a hash filename")
    if Path(filename).name != filename:
        raise RuntimeError(f"{row['id']}: invalid hash filename reported: {filename}")
    return filename


def _result_group_tag(row: dict, override: str) -> str:
    if override:
        return override
    result = row.get("result_json") or {}
    return str(result.get("group_tag") or "")


def _upload_command(*, playbook: Path, hash_file: Path, group_tag: str) -> list[str]:
    cmd = [
        "ansible-playbook",
        str(playbook),
        "-e",
        f"hash_file={hash_file}",
    ]
    if group_tag:
        cmd += ["-e", f"vm_group_tag={group_tag}"]
    return cmd


def main() -> int:
    args = _parse_args()
    hash_dir = Path(args.hash_dir)
    playbook = Path(args.playbook)
    if not hash_dir.exists():
        raise RuntimeError(f"Hash directory not found: {hash_dir}")
    if not playbook.exists():
        raise RuntimeError(f"Upload playbook not found: {playbook}")

    with db_pg.connection() as conn:
        agent_telemetry_pg.init(conn)
        rows = []
        for work_id in args.work_items:
            row = agent_telemetry_pg.get_work_item(conn, work_id)
            if row is None:
                raise RuntimeError(f"{work_id}: work item not found")
            if row["status"] != "complete":
                raise RuntimeError(f"{work_id}: work item is {row['status']}, not complete")
            rows.append(row)

    for row in rows:
        filename = _result_filename(row)
        hash_file = hash_dir / filename
        if not hash_file.exists():
            raise RuntimeError(f"{row['id']}: hash CSV not found: {hash_file}")
        group_tag = _result_group_tag(row, args.group_tag)
        cmd = _upload_command(playbook=playbook, hash_file=hash_file, group_tag=group_tag)
        print(f"{row['id']}: uploading {hash_file.name}", flush=True)
        subprocess.run(cmd, check=True)

    print(f"Uploaded {len(rows)} captured Autopilot hash file(s).", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
