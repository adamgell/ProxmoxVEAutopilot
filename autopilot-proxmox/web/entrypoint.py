"""Container process dispatcher.

The same image runs as `web`, `builder`, or `monitor` based on the
command-line arg (set by docker-compose `command:` key). Default mode
is `web` so operators on the pre-split compose spec keep working.

Design: docs/specs/2026-04-21-microservice-split-design.md §8
"""
from __future__ import annotations

import sys


def _run_web() -> None:
    """Launch the FastAPI/uvicorn server (existing entrypoint)."""
    import uvicorn
    from web.app import app
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")


def _run_builder() -> None:
    """Start the builder claim/run loop."""
    from web.builder import run_builder
    run_builder()


def _run_monitor() -> None:
    """Start the monitor singleton — sweep loop + keytab + reaper."""
    from web.monitor_main import run_monitor
    run_monitor()


_MODES = {
    "web": "_run_web",
    "builder": "_run_builder",
    "monitor": "_run_monitor",
}


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:]) if argv is None else argv
    mode = argv[0] if argv else "web"
    runner_name = _MODES.get(mode)
    if runner_name is None:
        print(f"unknown mode: {mode!r}. Valid: {sorted(_MODES)}", file=sys.stderr)
        sys.exit(2)
    # Look up the runner via the module so unittest.mock.patch() of
    # web.entrypoint._run_* takes effect.
    runner = getattr(sys.modules[__name__], runner_name)
    runner()


if __name__ == "__main__":
    main()
