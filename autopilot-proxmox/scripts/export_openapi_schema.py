#!/usr/bin/env python3
"""Export FastAPI OpenAPI schema from a local app import.

This intentionally does not depend on the live /openapi.json route. It is used
by the React migration to generate client types while keeping production
OpenAPI access behind the existing auth boundary.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        default="frontend/src/generated/openapi.json",
        help="Path to write the OpenAPI JSON schema.",
    )
    args = parser.parse_args()

    os.environ.setdefault("AUTOPILOT_AUTH_BYPASS", "1")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    from web.app import app

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
