#!/usr/bin/env python3
"""Wait for AutopilotAgent work items to reach a terminal state."""
from __future__ import annotations

import argparse
import sys
import time
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
        help="AutopilotAgent work item id to watch. May be repeated.",
    )
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--poll", type=int, default=5)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    deadline = time.monotonic() + args.timeout
    pending = set(args.work_items)
    last_status: dict[str, str] = {}

    print(f"Waiting for {len(pending)} AutopilotAgent work item(s).", flush=True)
    while pending:
        failed: list[dict] = []
        completed: list[str] = []
        with db_pg.connection() as conn:
            agent_telemetry_pg.init(conn)
            for work_id in sorted(pending):
                row = agent_telemetry_pg.get_work_item(conn, work_id)
                if row is None:
                    print(f"{work_id}: missing", flush=True)
                    return 2
                status = row["status"]
                if last_status.get(work_id) != status:
                    print(
                        f"{work_id}: {status} "
                        f"agent={row['agent_id']} kind={row['kind']} vmid={row.get('vmid')}",
                        flush=True,
                    )
                    last_status[work_id] = status
                if status == "complete":
                    completed.append(work_id)
                elif status == "failed":
                    failed.append(row)
        for work_id in completed:
            pending.remove(work_id)
        if failed:
            for row in failed:
                print(
                    f"{row['id']}: failed: {row.get('error') or 'no error reported'}",
                    flush=True,
                )
            return 1
        if not pending:
            break
        if time.monotonic() >= deadline:
            for work_id in sorted(pending):
                print(f"{work_id}: timed out waiting for agent completion", flush=True)
            return 124
        time.sleep(max(1, args.poll))
    print("All AutopilotAgent work item(s) completed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
