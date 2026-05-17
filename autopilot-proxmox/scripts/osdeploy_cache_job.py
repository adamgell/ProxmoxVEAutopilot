#!/usr/bin/env python3
"""OSDeploy cache worker entrypoint."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web import db_pg, osdeploy_cache


def _print(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OSDeploy cache worker")
    parser.add_argument("action", choices=["refresh", "warm", "verify", "delete"])
    parser.add_argument("--entry-id", default="")
    args = parser.parse_args(argv)
    with db_pg.connection() as conn:
        osdeploy_cache.init(conn)
        try:
            if args.action == "refresh":
                result = osdeploy_cache.refresh_catalog(conn)
            elif args.action == "warm":
                if not args.entry_id:
                    raise ValueError("--entry-id is required for warm")
                result = osdeploy_cache.warm_entry(conn, args.entry_id)
            elif args.action == "verify":
                if not args.entry_id:
                    raise ValueError("--entry-id is required for verify")
                result = osdeploy_cache.verify_entry(conn, args.entry_id)
            else:
                if not args.entry_id:
                    raise ValueError("--entry-id is required for delete")
                result = osdeploy_cache.delete_entry_file(conn, args.entry_id)
            _print({"ok": True, "result": result})
            return 0
        except Exception as exc:
            _print({"ok": False, "error": str(exc)})
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
