"""Curses-based TUI for launching the autopilot web service locally.

Why this exists: on macOS (UTM backend) the project runs natively, not in
Docker, so operators juggle `source .venv/bin/activate && python -m
web.entrypoint {web,builder,monitor}` in multiple terminals. This TUI
starts/stops those three processes from one screen and tails their logs.

State (PID files + log files) lives under .tui-state/ at the repo root of
autopilot-proxmox/ so it's colocated with the venv and gitignored by the
wrapper script.
"""
from __future__ import annotations

import curses
import os
import signal
import subprocess
import sys
import time
import webbrowser
from collections import deque
from pathlib import Path

MODES = ("web", "builder", "monitor")
# Default to 5055 on macOS to avoid the AirPlay Receiver on :5000
# (Control Center hijacks that port and returns HTTP 403 AirTunes).
# Override with AUTOPILOT_WEB_PORT in the environment if you've
# freed up 5000 or want a different port. Runtime "Change web port"
# action mutates this at runtime.
WEB_PORT = int(os.environ.get("AUTOPILOT_WEB_PORT", "5055"))

# Deps that app.py imports at top level. Missing any of these means
# `Start web` will crash in the first 300ms with ModuleNotFoundError,
# so we preflight them before spawning.
REQUIRED_MODULES = ("fastapi", "uvicorn", "urllib3", "jinja2", "yaml", "cryptography")

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = REPO_ROOT / ".tui-state"
STATE_DIR.mkdir(exist_ok=True)


def _web_url() -> str:
    return f"http://localhost:{WEB_PORT}"


def _venv_python() -> Path:
    return REPO_ROOT / ".venv" / "bin" / "python"


def _check_deps() -> list[str]:
    """Return the list of REQUIRED_MODULES that the venv can't import.

    Runs a subprocess so our own interpreter's modules don't mask a
    venv that's missing packages. Returns empty list if .venv/bin/python
    is absent (caller handles that case separately).
    """
    python = _venv_python()
    if not python.exists():
        return []
    probe = ";".join(f"import {m}" for m in REQUIRED_MODULES)
    res = subprocess.run(
        [str(python), "-c", probe],
        capture_output=True,
        text=True,
    )
    if res.returncode == 0:
        return []
    missing = []
    err = res.stderr
    for m in REQUIRED_MODULES:
        if f"No module named '{m}'" in err or f"No module named {m!r}" in err:
            missing.append(m)
    # If we can't attribute the failure to a specific module, return
    # all of them so the user is nudged to run `pip install -r`.
    return missing or list(REQUIRED_MODULES)


def _port_owner(port: int) -> int | None:
    """Return the PID listening on `port`, or None if nothing is.

    macOS-only: shells out to `lsof`. Returns None (not an exception)
    if lsof is missing so the TUI stays usable on minimal systems.
    """
    try:
        res = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    for line in res.stdout.splitlines():
        if line.startswith("p"):
            try:
                return int(line[1:])
            except ValueError:
                return None
    return None


def _pid_file(mode: str) -> Path:
    return STATE_DIR / f"{mode}.pid"


def _log_file(mode: str) -> Path:
    return STATE_DIR / f"{mode}.log"


def _read_pid(mode: str) -> int | None:
    p = _pid_file(mode)
    if not p.exists():
        return None
    try:
        pid = int(p.read_text().strip())
    except (ValueError, OSError):
        return None
    return pid if _pid_alive(pid) else None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _clear_stale_pid(mode: str) -> None:
    """Remove a PID file whose process is gone so status reflects reality."""
    p = _pid_file(mode)
    if p.exists() and _read_pid(mode) is None:
        try:
            p.unlink()
        except OSError:
            pass


def _start(mode: str) -> tuple[bool, str]:
    if _read_pid(mode) is not None:
        return False, f"{mode} already running"

    python = REPO_ROOT / ".venv" / "bin" / "python"
    if not python.exists():
        return False, f".venv missing at {python} — run scripts/run_macos_native.sh setup first"

    # Preflight: venv must have the packages app.py imports at top
    # level. Without this, uvicorn dies mid-import and the user sees
    # the confusing "exited immediately" path below.
    missing = _check_deps()
    if missing:
        return False, (
            f"missing Python deps: {', '.join(missing)} — "
            "run 'Install/update requirements' first"
        )

    # Preflight: someone else (often macOS AirPlay Receiver on :5000,
    # or a previous TUI session whose pidfile we lost) is already
    # bound to the port. Skip silently on systems without lsof —
    # _port_owner returns None there and we'd rather not false-fail.
    if mode == "web":
        owner = _port_owner(WEB_PORT)
        our_pid = _read_pid("web")
        if owner is not None and owner != our_pid:
            return False, (
                f":{WEB_PORT} already in use by pid {owner} — "
                "free it or use 'Change web port'"
            )

    # Truncate old log so the early-crash detector below doesn't show
    # yesterday's traceback as if it were today's failure.
    log_path = _log_file(mode)
    log_path.write_bytes(b"")
    log = log_path.open("ab", buffering=0)

    env = os.environ.copy()
    env.setdefault("AUTOPILOT_WEB_PORT", str(WEB_PORT))
    # Builder + monitor default to /app/output inside the Docker
    # image; on native macOS the root is read-only, so steer them at
    # repo-local directories (created below). Web doesn't need these
    # but exporting is harmless.
    output_dir = REPO_ROOT / "output"
    jobs_dir = REPO_ROOT / "jobs"
    output_dir.mkdir(exist_ok=True)
    jobs_dir.mkdir(exist_ok=True)
    env.setdefault("AUTOPILOT_OUTPUT_DIR", str(output_dir))
    env.setdefault("AUTOPILOT_JOBS_DIR", str(jobs_dir))
    # Local dev: if the vault.yml was copied from prod, auth_redirect_uri
    # points at the production hostname and Entra bounces the user there
    # after login. Override to match the local bind. The operator must
    # add this URL as a Redirect URI in the Entra app registration.
    env.setdefault(
        "AUTOPILOT_AUTH_REDIRECT_URI",
        f"http://localhost:{WEB_PORT}/auth/callback",
    )

    # start_new_session detaches from our controlling TTY so the child
    # survives TUI exit and doesn't receive our SIGINT on Ctrl+C.
    proc = subprocess.Popen(
        [str(python), "-m", "web.entrypoint", mode],
        cwd=str(REPO_ROOT),
        stdout=log,
        stderr=log,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )
    _pid_file(mode).write_text(str(proc.pid))

    # Early-crash detector: uvicorn / import errors show up within the
    # first second. Polling once at 0.8s avoids leaving a stale pidfile
    # and surfaces the last log line so the user doesn't have to open
    # the tail viewer to see why it failed.
    time.sleep(0.8)
    rc = proc.poll()
    if rc is not None:
        _pid_file(mode).unlink(missing_ok=True)
        tail = _tail(log_path, n=1)
        hint = tail[0] if tail else ""
        return False, f"{mode} exited immediately (rc={rc}): {hint[:120]}"

    suffix = f" on :{WEB_PORT}" if mode == "web" else ""
    return True, f"started {mode} (pid {proc.pid}){suffix}"


def _stop(mode: str) -> tuple[bool, str]:
    pid = _read_pid(mode)
    if pid is None:
        _clear_stale_pid(mode)
        return False, f"{mode} not running"
    try:
        # SIGTERM the whole process group so uvicorn's worker/reload
        # children (if any) also exit — start_new_session above gave
        # us a fresh pgid to target.
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        _pid_file(mode).unlink(missing_ok=True)
        return True, f"{mode} already exited"
    except PermissionError as e:
        return False, f"stop {mode}: {e}"

    for _ in range(30):
        if not _pid_alive(pid):
            break
        time.sleep(0.1)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    _pid_file(mode).unlink(missing_ok=True)
    return True, f"stopped {mode}"


def _tail(path: Path, n: int = 200) -> list[str]:
    if not path.exists():
        return []
    try:
        with path.open("rb") as f:
            try:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                # Read the last ~64 KiB — enough for n=200 lines in
                # normal logging without loading huge files.
                read_back = min(size, 64 * 1024)
                f.seek(size - read_back)
                data = f.read()
            except OSError:
                f.seek(0)
                data = f.read()
        lines = data.decode("utf-8", errors="replace").splitlines()
        return lines[-n:]
    except OSError:
        return []


# ----- curses UI -----

MENU_ITEMS = [
    ("Start web",           lambda: _start("web")),
    ("Start builder",       lambda: _start("builder")),
    ("Start monitor",       lambda: _start("monitor")),
    ("Stop web",            lambda: _stop("web")),
    ("Stop builder",        lambda: _stop("builder")),
    ("Stop monitor",        lambda: _stop("monitor")),
    ("Start all",           lambda: _start_all()),
    ("Stop all",            lambda: _stop_all()),
    ("Open web UI in browser", lambda: _open_browser()),
    ("Tail web log",        lambda: ("tail", "web")),
    ("Tail builder log",    lambda: ("tail", "builder")),
    ("Tail monitor log",    lambda: ("tail", "monitor")),
    ("Change web port…",    lambda: ("change_port", None)),
    ("Install/update requirements", lambda: ("pip_install", None)),
    ("Quit",                lambda: ("quit", None)),
]


def _start_all() -> tuple[bool, str]:
    msgs = []
    ok = True
    for m in MODES:
        s, msg = _start(m)
        ok = ok and s
        msgs.append(msg)
    return ok, "; ".join(msgs)


def _stop_all() -> tuple[bool, str]:
    msgs = []
    for m in MODES:
        _, msg = _stop(m)
        msgs.append(msg)
    return True, "; ".join(msgs)


def _open_browser() -> tuple[bool, str]:
    url = _web_url()
    try:
        webbrowser.open(url)
    except Exception as e:  # pragma: no cover — best effort
        return False, f"open browser: {e}"
    return True, f"opened {url}"


def _status_line(mode: str) -> str:
    pid = _read_pid(mode)
    return f"{mode:<8} {'RUNNING pid=' + str(pid) if pid else 'stopped'}"


def _draw_main(stdscr, sel: int, status: str, missing_deps: list[str]) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()

    title = "ProxmoxVEAutopilot — Launcher TUI"
    stdscr.addnstr(0, max(0, (w - len(title)) // 2), title, w, curses.A_BOLD)

    stdscr.addnstr(2, 2, "Services:", w - 2, curses.A_UNDERLINE)
    for i, m in enumerate(MODES):
        line = _status_line(m)
        attr = curses.color_pair(1) if _read_pid(m) else curses.color_pair(2)
        stdscr.addnstr(3 + i, 4, line, w - 4, attr)

    # Environment summary: the port may have been changed at runtime
    # and dep health is easy to break by nuking .venv — surface both.
    env_y = 3 + len(MODES)
    deps_ok = not missing_deps
    deps_label = "OK" if deps_ok else f"missing {len(missing_deps)}"
    deps_attr = curses.color_pair(1) if deps_ok else curses.color_pair(2)
    stdscr.addnstr(env_y, 4, f"Web port: {WEB_PORT}    Deps: ", w - 4)
    stdscr.addnstr(env_y, 4 + len(f"Web port: {WEB_PORT}    Deps: "),
                   deps_label, max(0, w - 4 - len(f"Web port: {WEB_PORT}    Deps: ")),
                   deps_attr)

    menu_top = env_y + 2
    stdscr.addnstr(menu_top - 1, 2, "Actions (↑/↓ select, Enter run, q quit):", w - 2, curses.A_UNDERLINE)
    for i, (label, _) in enumerate(MENU_ITEMS):
        y = menu_top + i
        if y >= h - 2:
            break
        attr = curses.A_REVERSE if i == sel else curses.A_NORMAL
        stdscr.addnstr(y, 4, f"{label:<30}", w - 4, attr)

    help_text = "↑/↓ or j/k move • Enter run • p change port • i install deps • q quit"
    if h >= 2:
        stdscr.addnstr(h - 2, 0, help_text[: w - 1], w - 1, curses.A_DIM)
    if status:
        stdscr.addnstr(h - 1, 0, status[: w - 1], w - 1, curses.A_DIM)
    stdscr.refresh()


def _draw_tail(stdscr, mode: str) -> None:
    """Live log viewer. q/Esc returns to the main menu."""
    curses.halfdelay(5)  # getch blocks up to 0.5s, drives refresh cadence
    log_path = _log_file(mode)
    try:
        while True:
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            header = f"Tailing {mode} log — {log_path}  (q to return)"
            stdscr.addnstr(0, 0, header[: w - 1], w - 1, curses.A_BOLD)
            lines = _tail(log_path, n=h - 2)
            buf = deque(lines, maxlen=h - 2)
            for i, line in enumerate(buf):
                stdscr.addnstr(1 + i, 0, line[: w - 1], w - 1)
            stdscr.refresh()
            ch = stdscr.getch()
            if ch in (ord("q"), 27):
                return
    finally:
        curses.cbreak()


def _pip_install(stdscr) -> tuple[bool, str]:
    """Run `pip install -r requirements.txt` with live output.

    We exit curses for the duration so pip's progress bars render
    correctly on the real TTY — trying to pipe them through curses
    produces garbled output. On return, the caller re-initializes
    the screen.
    """
    python = _venv_python()
    if not python.exists():
        return False, f".venv missing at {python}"
    req = REPO_ROOT / "requirements.txt"
    if not req.exists():
        return False, f"requirements.txt not found at {req}"

    curses.endwin()
    try:
        print("\n=== Installing/updating requirements (this takes a minute) ===\n")
        sys.stdout.flush()
        res = subprocess.run(
            [str(python), "-m", "pip", "install", "-r", str(req)],
            cwd=str(REPO_ROOT),
        )
    finally:
        # Force curses to reinitialize from scratch after endwin().
        stdscr.clear()
        stdscr.refresh()
    if res.returncode != 0:
        return False, f"pip install failed (rc={res.returncode}) — see terminal scrollback"
    return True, "requirements installed/updated"


def _prompt_port(stdscr) -> tuple[bool, str]:
    """Prompt the user for a new web port at the bottom of the screen.

    Refuses to change the port while any service is running — the
    running web process is bound to the old port and start/stop logic
    keys off WEB_PORT, so changing it mid-flight orphans pidfiles.
    """
    global WEB_PORT
    for m in MODES:
        if _read_pid(m) is not None:
            return False, "stop services before changing port"

    h, w = stdscr.getmaxyx()
    prompt = f"New web port (1024-65535, current {WEB_PORT}): "
    stdscr.move(h - 1, 0)
    stdscr.clrtoeol()
    stdscr.addnstr(h - 1, 0, prompt, w - 1)
    stdscr.refresh()

    curses.echo()
    curses.curs_set(1)
    try:
        raw = stdscr.getstr(h - 1, len(prompt), 10)
    finally:
        curses.noecho()
        curses.curs_set(0)

    text = (raw or b"").decode("utf-8", errors="replace").strip()
    if not text:
        return False, "port change cancelled"
    try:
        port = int(text)
    except ValueError:
        return False, f"not an integer: {text!r}"
    if not (1024 <= port <= 65535):
        return False, f"port {port} out of range (1024-65535)"
    WEB_PORT = port
    return True, f"web port set to {port}"




def _main(stdscr) -> None:
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_RED, -1)

    sel = 0
    status = ""
    # Cache the dep probe per main-loop iteration: it spawns a
    # subprocess and we redraw on every keystroke.
    missing_deps = _check_deps()
    while True:
        _draw_main(stdscr, sel, status, missing_deps)
        ch = stdscr.getch()
        if ch in (curses.KEY_UP, ord("k")):
            sel = (sel - 1) % len(MENU_ITEMS)
        elif ch in (curses.KEY_DOWN, ord("j")):
            sel = (sel + 1) % len(MENU_ITEMS)
        elif ch in (ord("q"), 27):
            return
        elif ch == ord("p"):
            ok, msg = _prompt_port(stdscr)
            status = ("✓ " if ok else "✗ ") + msg
        elif ch == ord("i"):
            ok, msg = _pip_install(stdscr)
            status = ("✓ " if ok else "✗ ") + msg
            missing_deps = _check_deps()
        elif ch in (curses.KEY_ENTER, 10, 13):
            label, action = MENU_ITEMS[sel]
            try:
                result = action()
            except Exception as e:  # pragma: no cover
                status = f"{label}: error: {e}"
                continue
            if isinstance(result, tuple) and result and result[0] == "quit":
                return
            if isinstance(result, tuple) and result and result[0] == "tail":
                _draw_tail(stdscr, result[1])
                status = f"tailed {result[1]} log"
            elif isinstance(result, tuple) and result and result[0] == "pip_install":
                ok, msg = _pip_install(stdscr)
                status = ("✓ " if ok else "✗ ") + msg
                missing_deps = _check_deps()
            elif isinstance(result, tuple) and result and result[0] == "change_port":
                ok, msg = _prompt_port(stdscr)
                status = ("✓ " if ok else "✗ ") + msg
            elif isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], bool):
                ok, msg = result
                status = ("✓ " if ok else "✗ ") + msg
            else:
                status = str(result)


def main() -> None:
    # Housekeeping: drop stale pidfiles so the first paint is accurate.
    for m in MODES:
        _clear_stale_pid(m)
    try:
        curses.wrapper(_main)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
