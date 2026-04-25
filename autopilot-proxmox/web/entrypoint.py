"""Container process dispatcher.

The same image runs as `web`, `builder`, or `monitor` based on the
command-line arg (set by docker-compose `command:` key). Default mode
is `web` so operators on the pre-split compose spec keep working.

Design: docs/specs/2026-04-21-microservice-split-design.md §8
"""
from __future__ import annotations

import sys


def _run_web() -> None:
    """Launch the FastAPI/uvicorn server (existing entrypoint).

    Port 5000 matches the Dockerfile EXPOSE and the single-container
    compose port mapping — changing the default would break every
    existing deploy's reverse-proxy config. On macOS, port 5000 is
    owned by Control Center's AirPlay Receiver, so operators running
    natively set AUTOPILOT_WEB_PORT (the TUI sets 5055 by default).
    """
    import os
    import uvicorn
    from web.app import app
    port = int(os.environ.get("AUTOPILOT_WEB_PORT", "5000"))
    host = os.environ.get("AUTOPILOT_WEB_HOST", "0.0.0.0")
    uvicorn.run(app, host=host, port=port, log_level="info")


def _configure_logging() -> None:
    """Ensure log lines land on stdout for docker logs visibility.

    Uvicorn owns logging in web mode; builder + monitor use plain
    `logging.getLogger(...)` calls and need basicConfig to wire up a
    stream handler. Without this, `docker logs autopilot-builder`
    returns empty even though the loop is running — confusing for ops.
    """
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )


def _paths_from_env() -> tuple[str, str]:
    """Resolve output + jobs dirs from env, defaulting to repo-relative paths.

    Docker keeps /app/output and /app/jobs as volumes; macOS-native
    operators (UTM backend) set AUTOPILOT_OUTPUT_DIR / AUTOPILOT_JOBS_DIR
    to repo-local paths (e.g. ./output, ./jobs) because "/" is
    read-only for non-root. The scripts/tui.sh and run_macos_native.sh
    wrappers set these automatically.

    Defaults are computed via web.paths so they resolve correctly in
    both Docker (/app/output) and native-macOS (repo/output) without
    any env-var wiring.
    """
    import os
    from web.paths import REPO_ROOT as _root
    out = os.environ.get("AUTOPILOT_OUTPUT_DIR", str(_root / "output"))
    jobs = os.environ.get("AUTOPILOT_JOBS_DIR", str(_root / "jobs"))
    return out, jobs


def _run_builder() -> None:
    """Start the builder claim/run loop."""
    _configure_logging()
    from pathlib import Path
    from web.builder import run_builder
    out, jobs = _paths_from_env()
    out_p = Path(out)
    out_p.mkdir(parents=True, exist_ok=True)
    Path(jobs).mkdir(parents=True, exist_ok=True)
    run_builder(
        jobs_dir=jobs,
        db_path=out_p / "jobs.db",
        monitor_db_path=out_p / "device_monitor.db",
    )


def _run_monitor() -> None:
    """Start the monitor singleton — sweep loop + keytab + reaper."""
    _configure_logging()
    from pathlib import Path
    from web.monitor_main import run_monitor
    out, _ = _paths_from_env()
    out_p = Path(out)
    out_p.mkdir(parents=True, exist_ok=True)
    run_monitor(
        lock_path=out_p / "monitor.lock",
        monitor_db_path=out_p / "device_monitor.db",
        jobs_db_path=out_p / "jobs.db",
    )


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
