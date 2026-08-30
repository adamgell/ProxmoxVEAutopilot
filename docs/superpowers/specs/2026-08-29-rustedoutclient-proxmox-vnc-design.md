# RustedOutClient Proxmox VNC Design

## Status

Approved direction, detailed design pending final user review before implementation.

## Decision Summary

RustedOutClient will be a macOS-first native Proxmox VE console client built from a fork of `hkder/ironvnc`. The fork keeps the useful RFB decoders, framebuffer renderer, input path, and egui shell, but removes the general-purpose VNC and SFTP product surfaces. Every initial connection is created from a validated Proxmox VM inventory entry and carried through a strictly verified system-SSH session to `qm vncproxy`.

The first release supports only VNC Authentication security type 2 inside that authenticated SSH transport. It rejects unauthenticated VNC, direct TCP endpoints, RA2, VeNCrypt, Apple Remote Desktop authentication, and unknown security types. A native connection streams RFB directly through the SSH child process without opening a local TCP port. TigerVNC remains available as an explicit loopback-only fallback until native acceptance is independently completed.

The product name, app title, target GitHub repository name, and Rust package name are `RustedOutClient`, `RustedOutClient`, `adamgell/RustedOutClient`, and `rustedoutclient`, respectively. The binary also implements the existing `pve-vnc list|open` command shape so the current workflow can migrate without changing operator habits.

## Baseline and Provenance

The implementation starts from upstream IronVNC commit `999e00e3a3672efdbf8e8f307e7bd60875dee67e`, observed on 2026-08-29. That snapshot is a single-commit Rust 2021 application using egui/eframe and Tokio. It contains Raw, CopyRect, Hextile, ZRLE, Tight, DesktopSize, cursor, keyboard, pointer, clipboard, VNC Authentication, and RA2 code. It also contains a plaintext `sessions.json` password store, direct host/port/password CLI options, an SFTP browser, permissive SFTP host-key verification, and no checked-in test or fuzz tree.

The fork must preserve the upstream MIT OR Apache-2.0 license files, authorship, and Git history. `upstream` remains configured as `https://github.com/hkder/ironvnc.git`. Upstream changes are reviewed and cherry-picked deliberately; the fork does not merge upstream automatically because upstream may reintroduce connection modes or credential behavior outside this design.

The existing local compatibility implementation remains the behavioral reference during migration:

- `/Users/Adam.Gell/.local/bin/pve-vnc`
- `/Users/Adam.Gell/.config/pve-vnc/config.json`
- `/Users/Adam.Gell/Desktop/Open PVE VNC.command`
- `/opt/homebrew/bin/vncviewer`

No existing helper, configuration, launcher, or TigerVNC installation is overwritten or removed during implementation or native acceptance.

## Goals

RustedOutClient must:

- display the configured Proxmox node's current QEMU VM inventory without requiring the operator to type a VMID;
- render cached non-secret inventory immediately and refresh it through a pre-warmed, strictly verified SSH control connection;
- open a running VM through a fresh `qm vncproxy` ticket with one click;
- support concurrent native console sessions in tabs;
- provide explicit Ctrl+Alt+Delete, release-all-keys, reconnect, fullscreen, fit-to-window, 1:1 scaling, and view-only controls;
- request a guest framebuffer that follows the usable console window through RFB ExtendedDesktopSize when the QEMU display device and guest driver support it, while retaining explicit scaled rendering when they do not;
- track key-down state and release guest modifiers when focus or connection state changes;
- store favorites, aliases, display preferences, and cached inventory without storing passwords, tickets, recovery material, or private keys;
- bound and validate every server-controlled protocol length before allocating or decoding;
- provide deterministic unit, integration, malformed-input, and fuzz coverage for the supported RFB subset;
- retain an explicit TigerVNC fallback through the same SSH trust boundary until native acceptance passes;
- leave the existing Python/TigerVNC workflow fully usable as rollback.

## Non-Goals

The initial release does not:

- manage VM power, snapshots, migration, storage, networking, or Proxmox configuration;
- connect to arbitrary VNC hosts or ports;
- expose a VNC listener on a LAN interface;
- support LXC consoles, SPICE, noVNC, RDP, VeNCrypt, RA2, RA2NE, Apple Remote Desktop authentication, or VNC security type None;
- persist SSH passwords, VNC passwords, Proxmox tickets, private keys, environment snapshots, clipboard contents, or command output containing secrets;
- provide SFTP, SCP, file transfer, drag-and-drop upload, or a remote filesystem browser;
- automatically trust a new or changed SSH host key;
- remove TigerVNC or the current `pve-vnc` Python helper;
- claim cross-platform acceptance beyond macOS arm64;
- automatically reconnect after a trust, authentication, or protocol failure;
- change VM display hardware, install or update guest video drivers, or claim that client-requested guest resizing works when QEMU reports it unsupported or the guest does not apply the request.

## Supported Platform and Toolchain

The first supported target is `aarch64-apple-darwin` on macOS 26. The repository pins Rust `1.92.0` in `rust-toolchain.toml`, uses Cargo's committed lockfile, and builds one native GUI/CLI binary named `rustedoutclient`.

TigerVNC fallback acceptance uses TigerVNC `vncviewer` 1.16.2 or newer. The fallback viewer path is configurable and must be an absolute path to a regular executable file.

The initial application delegates SSH transport to `/usr/bin/ssh`. This intentionally preserves OpenSSH configuration, agent use, private-key permissions, and known-hosts behavior already established on the Mac. An embedded SSH implementation is outside this design.

## Repository Ownership

`adamgell/RustedOutClient` owns all desktop-client code, tests, fuzz targets, packaging, and user documentation. `/Users/Adam.Gell/repo/ProxmoxVEAutopilot` owns this architecture and implementation plan because it is the umbrella project for the Proxmox lab and acceptance evidence.

The RustedOutClient implementation must not import ProxmoxVEAutopilot server code, database models, tokens, or credentials. Its only Proxmox interface is a fixed allowlist of read-only inventory and per-session console commands executed through SSH.

## High-Level Architecture

```text
RustedOutClient
├── egui desktop shell
│   ├── inventory and favorites
│   ├── native session tabs
│   ├── keyboard/menu actions
│   ├── dynamic-resolution policy and status
│   └── explicit TigerVNC fallback
├── session manager
│   ├── startup preloader
│   ├── active-session lifecycle
│   ├── non-secret cache
│   └── cleanup and reconnect
├── Proxmox transport
│   ├── strict OpenSSH command builder
│   ├── warm ControlMaster
│   ├── pvesh inventory client
│   ├── ephemeral qm vncproxy stream
│   └── loopback relay for fallback only
└── hardened RFB client
    ├── bounded wire parser
    ├── VNC Auth-only negotiation
    ├── supported encoding decoders
    ├── framebuffer and input state
    └── protocol tests and fuzz targets
```

The GUI thread never blocks on SSH, inventory, process startup, or RFB I/O. A single Tokio runtime owned by the application controller performs transport and session work and publishes typed events to the egui state model.

## Configuration and Persistence

The canonical configuration file is:

`~/Library/Application Support/RustedOutClient/config.json`

The non-secret inventory cache is `~/Library/Application Support/RustedOutClient/inventory-cache.json`. It is independently replaceable, never required to parse configuration, and may be deleted without losing favorites or preferences.

Its schema version is exactly 1:

```json
{
  "schema_version": 1,
  "profile": {
    "name": "Lab Proxmox",
    "ssh_target": "root@example.invalid",
    "node": "pve2"
  },
  "inventory_refresh_seconds": 15,
  "fallback_viewer": "/opt/homebrew/bin/vncviewer",
  "clipboard_enabled": false,
  "favorites": [],
  "display": {
    "scale_mode": "fit",
    "view_only": false
  }
}
```

`ssh_target` is always passed as one OpenSSH argument and never interpolated into a shell command. It must be 1-255 printable non-whitespace characters, contain no control character, and not begin with `-`. `node` must match `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`. `inventory_refresh_seconds` must be between 5 and 300. `fallback_viewer` must be absolute when configured.

Favorites contain only profile name, VMID, optional alias, preferred scale mode, preferred view-only state, and sort position. The inventory cache contains only VMID, VM name, node, status, template flag, and observation timestamp. Runtime session identifiers, tickets, process IDs, socket paths, clipboard text, and guest screen data are never serialized.

Configuration and cache writes are atomic: write a mode-0600 temporary file in the destination directory, `fsync`, rename, and keep the previous valid file if serialization or rename fails. The enclosing directory is mode 0700.

On first launch, RustedOutClient may read `~/.config/pve-vnc/config.json` to propose a schema-1 configuration. It imports only `ssh_target`, `node`, and `viewer`. It never alters or deletes the old file. Migration requires an explicit Save action after the user sees the resulting profile.

## SSH Trust Boundary

Every SSH command uses `/usr/bin/ssh` with these fixed options:

```text
-o BatchMode=yes
-o ConnectTimeout=12
-o ServerAliveInterval=15
-o ServerAliveCountMax=3
-o StrictHostKeyChecking=yes
-o PasswordAuthentication=no
-o KbdInteractiveAuthentication=no
```

RustedOutClient does not override `UserKnownHostsFile`, does not use `StrictHostKeyChecking=accept-new`, does not disable host-key algorithms, and does not accept host keys in application code. Existing OpenSSH configuration and `known_hosts` select and verify the key.

An unknown host key yields a `HostKeyUnknown` state with instructions to establish trust using OpenSSH outside RustedOutClient. A changed key yields a `HostKeyChanged` hard failure. Neither state offers an in-app bypass.

The startup preloader launches one foreground-owned OpenSSH ControlMaster child with `-M -N`, `ControlPersist=no`, and a control socket inside a randomly named mode-0700 directory under `/tmp`. The application retains the child handle. Read-only inventory and console proxy children use `-S <socket>` and cannot create a new unverified connection. Clean shutdown sends `ssh -O exit` and then terminates the owned master if necessary.

No SSH command is passed through `/bin/sh`, `zsh -c`, or another command interpreter. The only remote commands are:

```text
pvesh get /nodes/<validated-node>/qemu --output-format json
exec /usr/sbin/qm vncproxy <validated-decimal-vmid>
```

VMID is represented as `u32`, rendered as decimal, and accepted only in the Proxmox range 100-999999999. No caller-controlled text appears in the `qm vncproxy` command.

## Inventory and Preloading

Startup follows this sequence:

1. Load and validate schema-1 configuration.
2. Render favorites and the last non-secret inventory cache immediately with a visible stale timestamp.
3. Start the strict SSH ControlMaster in the background.
4. Confirm the master with `ssh -S <socket> -O check <target>`.
5. fetch `/nodes/<node>/qemu` inventory through the master;
6. atomically replace the cache and publish a live inventory event;
7. refresh every configured interval while the app is active.

Cached inventory is never proof that a VM is still running. Clicking Open revalidates the selected VM from a fresh inventory response before generating a ticket. A cached entry may be searched or favorited while offline, but it cannot create a console until live validation succeeds.

Preloading intentionally does not start `qm vncproxy`, generate VNC tickets, or establish hidden framebuffer sessions. Short-lived tickets are generated only for an explicit Open or Reconnect action. This keeps idle startup read-only and avoids invisible console consumers.

VM selection is exact and deterministic. A decimal selector matches VMID. A text selector matches VM name case-insensitively. Zero matches is an error; multiple name matches require VMID.

## Native Console Transport

For each native Open action:

1. Revalidate the VM and require status `running`.
2. Generate an eight-character ASCII alphanumeric ticket using the operating-system CSPRNG.
3. Spawn OpenSSH through the verified ControlMaster with `SendEnv=LC_PVE_TICKET` and the fixed `qm vncproxy` command.
4. Put the ticket only in that child process's `LC_PVE_TICKET` environment value.
5. Connect the RFB client directly to the child process's stdin/stdout byte stream.
6. Drop and zeroize the local ticket value after authentication setup.
7. Keep the child handle attached to the active session.
8. On close, EOF, error, or app shutdown, close stdin, terminate the child, wait up to three seconds, and kill only that owned child if it remains.

The embedded path never binds a TCP listener. stderr is captured into a bounded 64-KiB diagnostic buffer that redacts target addresses and never captures environment values. A session error names the failed phase: SSH master, inventory, proxy spawn, RFB banner, security negotiation, server initialization, encoding decode, input send, or cleanup.

## RFB Security Policy

The supported RFB versions are 3.3, 3.7, and 3.8. A higher recognizable version is capped at 3.8. A malformed 12-byte banner is rejected; it is not silently treated as 3.3.

The only accepted RFB security type is VNC Authentication type 2, and it is accepted only when the transport carries an internal `TrustedSshProxy` marker created by the Proxmox transport module. Security type None is always rejected. RA2 types 5, 6, 129, and 130 are removed from the compiled product. VeNCrypt, ARD, and unknown types are rejected with an allowlist error.

The client does not expose host, port, username, or password fields for VNC. It removes upstream `--host`, `--port`, `--password`, `--session`, `--test`, and `--sftp-test` paths. No password can appear in process arguments.

VNC's DES challenge-response remains because `qm vncproxy` requires it. It is not treated as transport encryption. Confidentiality and server identity come from the verified SSH connection.

## Protocol Limits

All integer arithmetic uses checked operations. A value over a limit closes the session before allocation or decompression. The parser never clamps a declared length and continues on a desynchronized stream.

Initial limits are:

| Input | Limit |
|---|---:|
| RFB banner | exactly 12 ASCII bytes |
| Desktop width or height | 1-8192 |
| Total framebuffer pixels | 33,554,432 |
| Framebuffer RGBA allocation | 134,217,728 bytes |
| Desktop name | 65,536 bytes |
| Authentication failure reason | 65,536 bytes |
| Rectangles per framebuffer update | 4,096 |
| Encoded rectangle payload | 67,108,864 bytes |
| Decompressed rectangle RGBA | bounded by rectangle dimensions and framebuffer limit |
| Clipboard text | 1,048,576 UTF-8 bytes |
| Captured SSH stderr | 65,536 bytes |
| Pending UI events per session | bounded channel of 256 events |

Every rectangle must have non-zero dimensions, fit inside the current framebuffer, and pass checked byte-count calculations. DesktopSize must pass the same global dimension and pixel limits before replacing the framebuffer. CopyRect source and destination bounds are both validated.

Raw, Hextile, ZRLE, and Tight decoders validate encoded lengths, palette indexes, run lengths, tile coordinates, row strides, zlib progress, decompressed output size, JPEG dimensions, and destination bounds. Unknown encodings terminate the session without trying to skip an unknown number of bytes.

## Input and Secure Attention

The input controller owns a set of currently pressed RFB keysyms. Normal keyboard events update that set before transmission. Focus loss, view-only activation, disconnect, RFB error, and app shutdown all call `release_all_keys()`.

Ctrl+Alt+Delete is a named action available from both the Session menu and toolbar. It sends exactly:

```text
KeyDown Control_L  0xFFE3
KeyDown Alt_L      0xFFE9
KeyDown Delete     0xFFFF
KeyUp   Delete     0xFFFF
KeyUp   Alt_L      0xFFE9
KeyUp   Control_L  0xFFE3
```

The action is disabled in view-only mode and while the session is not Ready. A separate Release All Keys action sends key-up for every tracked keysym and clears the set. Unit tests assert event ordering and cleanup after an injected write failure.

## Clipboard

Automatic bidirectional clipboard synchronization is disabled by default. The initial release supports explicit text-only Send Clipboard and Receive Clipboard actions when clipboard is enabled in session preferences. Each transfer is capped at 1 MiB, displayed as an explicit operator action, and never logged or persisted. File clipboard formats and drag-and-drop are unsupported.

## Dynamic Guest Resolution

Fit to Window scales pixels locally; it does not change the guest framebuffer. RustedOutClient separately supports a Dynamic Resolution mode that requests a guest framebuffer matching the usable viewport when the QEMU VNC/display backend and guest video driver support the RFB ExtendedDesktopSize flow. Dynamic Resolution starts enabled for every new native session so the first stable viewport is attempted automatically after the session becomes Ready; the checked View-menu/toolbar control can disable it for that session or manually retry after a capability failure.

The client advertises both DesktopSize (`-223`) and ExtendedDesktopSize (`-308`). After the session is Ready and a stable viewport is known, Dynamic Resolution may send the standard one-screen SetDesktopSize client message (`251`). The initial subset accepts exactly one returned screen at origin `(0,0)` and rejects malformed or unsupported multi-screen layouts. Requests are constrained by the same 8,192-by-8,192 and 33,554,432-pixel framebuffer limits, never go below 640-by-480, and are rounded down to whole multiples of eight. The UI uses the viewport's backing-pixel dimensions, with those limits applied, so a Retina window is not silently fixed to its 1,280-by-800 logical-point size.

Viewport changes are debounced for 250 ms. Only one request may be in flight; a newer desired size replaces the pending follow-up rather than adding another queue entry. A QEMU `Request forwarded` result is Pending, not proof that Windows changed resolution. Success requires a later valid DesktopSize/ExtendedDesktopSize update with the requested dimensions. A rejection, unsupported-layout result, or two-second no-change timeout disables automatic requests for that session and leaves Fit to Window available. The status bar shows Requested, Pending, Applied, Rejected, Unsupported, or Timed out without disconnecting an otherwise healthy console. Malformed resize messages still fail closed as protocol errors.

QEMU's VNC dispatcher accepts SetDesktopSize but forwards it only when the selected display backend reports UI-resize support; this is the reason capability rejection and guest no-change are first-class states rather than connection failures ([QEMU `ui/vnc.c`](https://gitlab.com/qemu-project/qemu/-/blob/v7.2.9/ui/vnc.c?ref_type=tags)). Dynamic Resolution never runs `qm set`, edits VM configuration, chooses a different emulated display adapter, or installs a guest driver. Native acceptance records the existing VM display device and observed response. If the target Windows guest cannot apply forwarded requests, changing its virtual display/driver is a separate explicitly approved lab change.

## Session Manager and UI

The main window contains:

- a searchable inventory/favorites sidebar with running, stopped, stale, connecting, ready, and error states;
- a tab strip for active native sessions;
- a central framebuffer viewport;
- a status bar showing profile, VMID, VM name, live/stale inventory age, session phase, scale mode, guest framebuffer dimensions, dynamic-resolution state, view-only state, and clipboard state;
- a Session menu and compact toolbar.

The Session menu contains Open, Reconnect, Close, Open in TigerVNC, Ctrl+Alt+Delete, Release All Keys, View Only, Fit to Window, 1:1, Fullscreen, Send Clipboard, Receive Clipboard, and Diagnostics. The View menu and toolbar expose Dynamic Resolution as a per-session checked control that starts enabled. Unsafe actions are disabled when their preconditions are false.

Opening an already active VM focuses its tab instead of creating a duplicate proxy. Reconnect always creates a fresh ticket and child process after cleaning the previous owned resources. Multiple different VMs may be active concurrently.

The GUI displays typed, non-secret failures and a Copy Diagnostics action. Diagnostics include app version, upstream base SHA, OS/architecture, profile display name, node, VMID, state transitions, duration, child exit status, and error category. They exclude SSH target, IP address, username, host fingerprint, tickets, environment values, clipboard content, guest pixels, and raw SSH stderr.

## TigerVNC Fallback

TigerVNC is explicit, not automatic. A protocol failure presents Open in TigerVNC but does not silently switch paths, so native failures remain visible and testable.

The fallback path:

1. revalidates the selected running VM;
2. generates a fresh eight-character ticket;
3. writes only the TigerVNC obfuscated eight-byte password file to a mode-0600 temporary file inside a mode-0700 runtime directory;
4. binds a random listener on `127.0.0.1` only;
5. launches the configured viewer with `-Shared=1`, `-RemoteResize=1`, `-SecurityTypes=VncAuth`, and `-PasswordFile`;
6. accepts one loopback viewer connection;
7. relays it to an SSH `qm vncproxy` child using the verified ControlMaster;
8. removes the password file and terminates owned listener/proxy resources on every exit path.

The fallback must reject a non-loopback bind address and a viewer path that is relative, missing, not regular, or not executable. It never writes the cleartext ticket to disk.

The old Python helper and Desktop launcher remain the final rollback path throughout initial acceptance. Removing them requires a separate approved change after a sustained native-use period.

## Error Handling and Cleanup

Typed top-level errors are Config, HostKeyUnknown, HostKeyChanged, SshUnavailable, SshAuthentication, Inventory, VmNotFound, VmNotRunning, Proxy, RfbProtocol, RfbSecurity, RfbLimit, Decoder, ViewerFallback, and Cleanup.

Errors never contain environment dumps or unbounded remote text. Authentication failure reasons and server desktop names are sanitized to printable text and bounded before display.

All child processes, channels, runtime directories, listeners, and temporary files have one owning session or application object. Cleanup is idempotent. Dropping a session initiates graceful closure, waits no longer than three seconds per child, and kills only recorded child PIDs that remain. RustedOutClient never searches for or terminates unrelated `ssh`, `vncviewer`, or Python processes.

## Testing Strategy

### Unit tests

Unit tests cover configuration validation and atomic persistence, migration without secret fields, selector behavior, OpenSSH argument construction, fixed remote commands, host-key error classification, ticket generation shape, log redaction, protocol limits, security allowlisting, all supported decoders, framebuffer bounds, DesktopSize and ExtendedDesktopSize parsing, bounded SetDesktopSize construction and debounce state, input event ordering, release-all behavior, session state transitions, and diagnostics redaction.

### Integration tests

A fake OpenSSH executable records arguments to a test-owned temporary directory and returns fixture inventory or a synthetic RFB stream. Tests prove:

- strict options are present on master, inventory, proxy, and fallback commands;
- target and node values remain single arguments;
- no shell is invoked;
- ticket material is absent from argv, logs, diagnostics, configuration, and cache;
- inventory is cached but revalidated before Open;
- native RFB uses child stdin/stdout without a listener;
- fallback binds only loopback and deletes its password file;
- failed or cancelled sessions leave no owned child or temporary artifact.

A deterministic in-process RFB peer exercises a complete supported handshake, one framebuffer update per encoding, pointer input, normal keys, Ctrl+Alt+Delete, clipboard limits, DesktopSize, malformed lengths, truncated streams, and disconnect cleanup.

### Fuzzing

`cargo-fuzz` targets parse:

- RFB protocol and security banners;
- ServerInit and desktop-name framing;
- framebuffer update and rectangle headers;
- Raw, Hextile, ZRLE, and Tight payloads;
- server clipboard messages.

The invariant is no panic, abort, unbounded allocation, infinite loop, or out-of-bounds framebuffer write. CI runs each target against its committed seed corpus for a bounded smoke duration; scheduled local security runs execute longer campaigns.

### Static and supply-chain gates

Every change passes:

```text
cargo fmt --all -- --check
cargo test --all-targets --all-features
cargo clippy --all-targets --all-features -- -D warnings
cargo audit
cargo deny check
```

`deny.toml` denies known vulnerabilities, unmaintained/yanked crates unless explicitly reviewed with an expiry, unknown registries, git dependencies, and licenses outside the reviewed allowlist. Release builds use the committed `Cargo.lock` and do not download code at runtime.

## Performance and Native Acceptance

Timing is measured from the Open action to the first rendered non-empty framebuffer update. On the same Mac, node, running VM, and LAN:

- cached inventory must render within 250 ms of app launch;
- live inventory must replace the cache within 3 seconds when the SSH master is already trusted and reachable;
- ten warm native opens must have a median first-frame time no greater than 2 seconds;
- the warm native median must be at least 30 percent faster than ten opens through the existing Python/TigerVNC launcher;
- the app must remain responsive while two native sessions update concurrently.

Native acceptance also requires observed, visible proof of:

- correct inventory and exact VM selection;
- framebuffer correctness through the Windows boot, lock, and desktop surfaces;
- pointer movement and click accuracy;
- normal keyboard input with no stuck modifiers;
- Ctrl+Alt+Delete producing the expected Windows secure-attention transition;
- Release All Keys recovering an injected held modifier;
- fullscreen, fit, 1:1, resize, and view-only behavior;
- Dynamic Resolution at multiple viewport sizes when the existing QEMU display/guest driver supports it, or a truthful Unsupported/Timed out state with uninterrupted Fit to Window when it does not;
- explicit clipboard default-off behavior and bounded text transfer when enabled;
- reconnect after proxy termination with a fresh ticket;
- two concurrent VM sessions;
- ten connect/disconnect cycles with no orphaned child, listener, ticket file, or runtime directory owned by completed sessions;
- TigerVNC fallback through loopback and SSH;
- unchanged operation of the old Python helper.

Passing unit tests, fuzz smoke tests, or a successful TCP connection is not native acceptance. Acceptance evidence records exact RustedOutClient commit, build hash, app version, Mac architecture, configured profile name, VMIDs tested, timestamps, timing samples, and observed UI transitions without recording credentials or guest screen content.

## Rollout and Rollback

RustedOutClient installs beside the existing helper as `/Users/Adam.Gell/.local/bin/rustedoutclient` or a signed macOS app bundle. The first rollout does not replace `/Users/Adam.Gell/.local/bin/pve-vnc` and does not edit the Desktop launcher.

After native acceptance, a separate reversible cutover may update the Desktop launcher or install a `pve-vnc` shim that invokes RustedOutClient. TigerVNC fallback and the Python helper remain available until a separately approved retirement change.

Rollback consists of closing RustedOutClient and invoking the existing Python helper. No server-side rollback is required because this design makes no Proxmox configuration change and leaves no persistent console ticket.

## Security Acceptance Checklist

- [ ] Upstream base SHA and license provenance are recorded.
- [ ] SFTP code and dependencies are absent from the build.
- [ ] Direct VNC host/port/password and saved-password paths are absent.
- [ ] Configuration and cache contain no secret-bearing field.
- [ ] SSH host verification is strict and has no bypass.
- [ ] SSH passwords are disabled; agent/key authentication is used.
- [ ] Native RFB has no local TCP listener.
- [ ] VNC Authentication is accepted only over `TrustedSshProxy`.
- [ ] Security None, RA2, VeNCrypt, ARD, and unknown modes are rejected.
- [ ] All declared protocol lengths are checked before allocation.
- [ ] Protocol parsers and decoders pass unit, integration, and fuzz smoke gates.
- [ ] Tickets are absent from argv, disk, configuration, logs, and diagnostics.
- [ ] Fallback listener is loopback-only and its password file is always removed.
- [ ] Clipboard is default-off, explicit, bounded, and non-persistent.
- [ ] Ctrl+Alt+Delete and Release All Keys are visibly accepted on Windows.
- [ ] Dynamic Resolution is bounded, debounced, and visibly applied on the target Windows guest, or the exact unsupported device/driver boundary is recorded without changing VM configuration.
- [ ] No owned process or temporary artifact remains after repeated disconnects.
- [ ] Existing TigerVNC/Python rollback remains operational.

## Documentation Deliverables

The implementation repository contains:

- `README.md` with supported scope and operator workflow;
- `SECURITY.md` with reporting instructions and the SSH/RFB trust boundary;
- `docs/threat-model.md` covering assets, trust boundaries, attacker capabilities, and mitigations;
- `docs/configuration.md` documenting schema 1 without real infrastructure values;
- `docs/native-acceptance.md` with evidence-safe acceptance commands and observations;
- `docs/upstream.md` recording the fork base and reviewed upstream-update process.

No document includes real credentials, private keys, host fingerprints, tickets, private URLs, clipboard text, or guest screen captures.
