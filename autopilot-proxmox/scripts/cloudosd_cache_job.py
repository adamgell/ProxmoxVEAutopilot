#!/usr/bin/env python3
"""CloudOSD cache worker entrypoint."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web import cloudosd_cache, db_pg


def _print(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CloudOSD cache worker")
    parser.add_argument("action", choices=["refresh", "warm", "verify", "delete"])
    parser.add_argument("--entry-id", default="")
    parser.add_argument("--osdcloud-module-version", default="26.4.17.1")
    args = parser.parse_args(argv)

    with db_pg.connection() as conn:
        cloudosd_cache.init(conn)
        try:
            if args.action == "refresh":
                result = cloudosd_cache.refresh_catalog(
                    conn,
                    module_version=args.osdcloud_module_version,
                )
            elif args.action == "warm":
                if not args.entry_id:
                    raise ValueError("--entry-id is required for warm")
                result = cloudosd_cache.warm_entry(conn, args.entry_id)
            elif args.action == "verify":
                if not args.entry_id:
                    raise ValueError("--entry-id is required for verify")
                result = cloudosd_cache.verify_entry(conn, args.entry_id)
            else:
                if not args.entry_id:
                    raise ValueError("--entry-id is required for delete")
                result = cloudosd_cache.delete_entry_file(conn, args.entry_id)
            _print({"ok": True, "result": result})
            return 0
        except Exception as exc:
            _print({"ok": False, "error": str(exc)})
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
