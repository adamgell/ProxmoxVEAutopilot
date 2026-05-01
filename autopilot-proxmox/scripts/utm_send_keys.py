#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
utm_send_keys.py - Send keystrokes to a UTM-managed QEMU VM via SPICE.

Bypasses macOS Automation permission entirely by talking directly to
UTM's SPICE unix socket and using SpiceInputsChannel to send PC XT
set-1 scancodes.

Use case: drive a VM at the EFI shell / pre-OS stage where there is
no guest agent yet. Replaces an `osascript "input keystroke"` flow
that fails with `Application isn't running (-600)` when UTM lacks
macOS Automation permission.

Why SpiceInputsChannel and not QMP send-key:
  UTM exposes QMP as a SPICE port (chardev=spiceport,
  name=org.qemu.monitor.qmp.0), not as a raw QMP unix socket. To use
  QMP we'd have to negotiate the SPICE link, open the named port,
  then frame JSON over it. The SPICE inputs channel is part of the
  same SPICE link and sends keystrokes directly - one fewer layer.

Socket location:
  UTM stores its SPICE unix sockets in the app-group container at
    ~/Library/Group Containers/WDNLXAD4W8.com.utmapp.UTM/<UUID>.spice
  Sockets only exist while the VM is running. The path is auto-
  discovered from the VM UUID via --vm-uuid; pass --socket to
  override.

Usage:
    utm_send_keys.py --vm-uuid <UUID> < spec.txt
    utm_send_keys.py --socket /path/to/<UUID>.spice < spec.txt
    echo "KEY Enter" | utm_send_keys.py --vm-uuid <UUID>

Spec language (one directive per line, # for comments):
    KEY <name>          Press+release a single named key.
                          Names: Enter, Esc, Tab, Space, BkSp, Shift
    TEXT <literal>      Type a literal ASCII string.
                          Supports backslash, colon, period, digits,
                          letters (lower+upper). Each char becomes
                          press+release; uppercase + symbol-shift
                          chars wrap with Shift.
    DELAY <seconds>     Sleep N seconds (float OK).

Exit codes:
    0  all directives sent successfully
    2  bad arguments / spec parse error
    3  SPICE socket not found
    4  SPICE link / channel setup failed
"""

import argparse
import os
import pathlib
import sys
import time
import warnings

# spice-gtk 0.42 (current macOS brew) marks inputs_key_press_and_release
# as deprecated even though no replacement is exposed via PyGObject in
# this version. Silence to keep logs clean - revisit when spice-gtk
# ships a `qkey` / `qcode` variant in introspection.
warnings.filterwarnings("ignore",
                        category=DeprecationWarning,
                        module="gi")

import gi
gi.require_version("SpiceClientGLib", "2.0")
from gi.repository import GLib, GObject, SpiceClientGLib  # noqa: E402

# Default socket directory. UTM ties this to its team-id-prefixed
# app-group container. Stable across UTM versions to date.
DEFAULT_SOCKET_DIR = pathlib.Path.home() / "Library" / "Group Containers" / "WDNLXAD4W8.com.utmapp.UTM"

# PC XT set-1 scancodes for the keys we actually use.
# Source: https://wiki.osdev.org/PS/2_Keyboard
# Format: { ascii_char: (scancode, needs_shift) }
SHIFT_SC = 0x2A

KEY_TABLE = {
    # --- digits row ---
    "1": (0x02, False), "!": (0x02, True),
    "2": (0x03, False), "@": (0x03, True),
    "3": (0x04, False), "#": (0x04, True),
    "4": (0x05, False), "$": (0x05, True),
    "5": (0x06, False), "%": (0x06, True),
    "6": (0x07, False), "^": (0x07, True),
    "7": (0x08, False), "&": (0x08, True),
    "8": (0x09, False), "*": (0x09, True),
    "9": (0x0A, False), "(": (0x0A, True),
    "0": (0x0B, False), ")": (0x0B, True),
    "-": (0x0C, False), "_": (0x0C, True),
    "=": (0x0D, False), "+": (0x0D, True),
    # --- top alpha row ---
    "q": (0x10, False), "w": (0x11, False), "e": (0x12, False),
    "r": (0x13, False), "t": (0x14, False), "y": (0x15, False),
    "u": (0x16, False), "i": (0x17, False), "o": (0x18, False),
    "p": (0x19, False),
    "[": (0x1A, False), "{": (0x1A, True),
    "]": (0x1B, False), "}": (0x1B, True),
    # --- middle alpha row ---
    "a": (0x1E, False), "s": (0x1F, False), "d": (0x20, False),
    "f": (0x21, False), "g": (0x22, False), "h": (0x23, False),
    "j": (0x24, False), "k": (0x25, False), "l": (0x26, False),
    ";": (0x27, False), ":": (0x27, True),
    "'": (0x28, False), '"': (0x28, True),
    "`": (0x29, False), "~": (0x29, True),
    "\\": (0x2B, False), "|": (0x2B, True),
    # --- bottom alpha row ---
    "z": (0x2C, False), "x": (0x2D, False), "c": (0x2E, False),
    "v": (0x2F, False), "b": (0x30, False), "n": (0x31, False),
    "m": (0x32, False),
    ",": (0x33, False), "<": (0x33, True),
    ".": (0x34, False), ">": (0x34, True),
    "/": (0x35, False), "?": (0x35, True),
    " ": (0x39, False),
}
# Uppercase letters: same scancode, with Shift.
for _c in "abcdefghijklmnopqrstuvwxyz":
    KEY_TABLE[_c.upper()] = (KEY_TABLE[_c][0], True)

NAMED_KEYS = {
    "ENTER": 0x1C,
    "ESC":   0x01,
    "TAB":   0x0F,
    "SPACE": 0x39,
    "BKSP":  0x0E,
    "SHIFT": SHIFT_SC,
}


def discover_socket(vm_uuid: str) -> pathlib.Path:
    path = DEFAULT_SOCKET_DIR / f"{vm_uuid}.spice"
    if not path.exists():
        sys.exit(f"ERROR: SPICE socket not found: {path}\n"
                 f"       (Is the VM running? utmctl status {vm_uuid})")
    return path


def parse_spec(text: str):
    """Yield (op, arg) tuples. op in {'KEY','TEXT','DELAY'}. Skips blanks/#."""
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if " " in line:
            op, arg = line.split(" ", 1)
        else:
            op, arg = line, ""
        op = op.upper()
        if op == "KEY":
            name = arg.strip().upper()
            if name not in NAMED_KEYS:
                sys.exit(f"ERROR: line {lineno}: unknown KEY name {name!r}; "
                         f"valid: {sorted(NAMED_KEYS)}")
            yield ("KEY", NAMED_KEYS[name])
        elif op == "TEXT":
            for ch in arg:
                if ch not in KEY_TABLE:
                    sys.exit(f"ERROR: line {lineno}: no scancode mapped for "
                             f"character {ch!r}")
            yield ("TEXT", arg)
        elif op == "DELAY":
            try:
                yield ("DELAY", float(arg))
            except ValueError:
                sys.exit(f"ERROR: line {lineno}: bad DELAY value {arg!r}")
        else:
            sys.exit(f"ERROR: line {lineno}: unknown directive {op!r}")


def send_directives(inputs_channel, directives, inter_key_delay: float = 0.02):
    """Replay directives on an open SpiceInputsChannel.

    inter_key_delay throttles successive keystrokes so the guest input
    handler doesn't drop characters under load. 20 ms is conservative
    and adds 100 ms total for our typical 5-keystroke EFI sequence.
    """
    for op, arg in directives:
        if op == "KEY":
            SpiceClientGLib.inputs_key_press_and_release(inputs_channel, arg)
            time.sleep(inter_key_delay)
        elif op == "TEXT":
            for ch in arg:
                sc, shifted = KEY_TABLE[ch]
                if shifted:
                    SpiceClientGLib.inputs_key_press(inputs_channel, SHIFT_SC)
                SpiceClientGLib.inputs_key_press_and_release(inputs_channel, sc)
                if shifted:
                    SpiceClientGLib.inputs_key_release(inputs_channel, SHIFT_SC)
                time.sleep(inter_key_delay)
        elif op == "DELAY":
            time.sleep(arg)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--vm-uuid",
                        help="UTM VM UUID (used to locate the SPICE socket)")
    parser.add_argument("--socket", type=pathlib.Path,
                        help="Override SPICE unix socket path")
    parser.add_argument("--spec-file", type=pathlib.Path,
                        help="Read spec from a file instead of stdin")
    parser.add_argument("--connect-timeout", type=float, default=10.0,
                        help="Seconds to wait for InputsChannel (default 10)")
    args = parser.parse_args()

    if not args.socket and not args.vm_uuid:
        parser.error("must pass --vm-uuid or --socket")
    socket_path = args.socket or discover_socket(args.vm_uuid)

    spec_text = args.spec_file.read_text() if args.spec_file else sys.stdin.read()
    directives = list(parse_spec(spec_text))
    if not directives:
        sys.exit("ERROR: empty spec")

    # macOS sockaddr_un.sun_path is 104 bytes. UTM's sockets live at
    #   ~/Library/Group Containers/WDNLXAD4W8.com.utmapp.UTM/<UUID>.spice
    # which is ~110 chars and exceeds that. Both Python's socket and
    # spice-gtk's g_socket_client_connect_async fail silently with
    # "No such file or directory" on the truncated path. The standard
    # workaround is to chdir to the parent directory and pass just the
    # basename - matches what UTM's own QEMULauncher does (it binds
    # the socket from cwd).
    socket_path = socket_path.resolve()
    os.chdir(socket_path.parent)
    relative_socket = socket_path.name

    # Connect to the SPICE unix socket. UTM's defaults: ticketing
    # disabled (auth=none), gl=on, image-compression=off. We don't
    # care about display channels - we just need MainChannel to come
    # up so InputsChannel can attach. Setting `disable-effects` and
    # ignoring DisplayChannel keeps the client lightweight.
    session = SpiceClientGLib.Session()
    session.set_property("unix-path", relative_socket)
    session.set_property("password", "")  # disable-ticketing=on -> empty pw

    loop = GLib.MainLoop()
    state = {"inputs": None, "error": None}

    def on_channel_new(_session, channel):
        if isinstance(channel, SpiceClientGLib.InputsChannel):
            # SpiceChannel also overrides .connect() (network-level
            # connect, no signal-binding form), so reach the GObject
            # base class to attach the channel-event handler.
            def on_event(_ch, event):
                if event == SpiceClientGLib.ChannelEvent.OPENED:
                    state["inputs"] = channel
                    loop.quit()
                elif event in (SpiceClientGLib.ChannelEvent.ERROR_CONNECT,
                               SpiceClientGLib.ChannelEvent.ERROR_TLS,
                               SpiceClientGLib.ChannelEvent.ERROR_LINK,
                               SpiceClientGLib.ChannelEvent.ERROR_AUTH,
                               SpiceClientGLib.ChannelEvent.ERROR_IO):
                    state["error"] = f"InputsChannel error event: {event}"
                    loop.quit()
            GObject.GObject.connect(channel, "channel-event", on_event)
            # Kick the channel network handshake. Returns bool sync;
            # actual readiness is signalled by channel-event=OPENED.
            channel.connect()

    # SpiceSession overrides .connect() with its own network-connect
    # method, shadowing the GObject signal-binder. Reach the base class
    # explicitly to bind the channel-new signal.
    GObject.GObject.connect(session, "channel-new", on_channel_new)
    if not session.connect():
        sys.exit("ERROR: SpiceSession.connect() returned False")

    # Bail out if the InputsChannel never shows up.
    GLib.timeout_add(int(args.connect_timeout * 1000),
                     lambda: (state.update(error="timeout waiting for InputsChannel")
                              or loop.quit() or False))
    loop.run()

    if state["error"]:
        sys.exit(f"ERROR: {state['error']}")
    if state["inputs"] is None:
        sys.exit("ERROR: no InputsChannel arrived (unexpected)")

    send_directives(state["inputs"], directives)

    # SPICE inputs_key_press_and_release queues a SpiceMsgcKeyPressRelease
    # via the channel's outbound coroutine and returns synchronously. If
    # we session.disconnect() before the coroutine has had a chance to
    # write the message to the socket, the keystrokes vanish silently.
    # Drain the GLib mainloop for a beat so pending output flushes.
    drain_loop = GLib.MainLoop()
    GLib.timeout_add(500, lambda: (drain_loop.quit() or False))
    drain_loop.run()

    session.disconnect()


if __name__ == "__main__":
    main()
