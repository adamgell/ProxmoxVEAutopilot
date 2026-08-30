# RustedOutClient Proxmox VNC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fork IronVNC into RustedOutClient, reduce it to a hardened macOS Proxmox-over-SSH console client, preload non-secret session state, add reliable console controls, and preserve TigerVNC as an explicit rollback path.

**Architecture:** RustedOutClient uses a strict system-OpenSSH ControlMaster for cached inventory and ephemeral `qm vncproxy` streams. The embedded RFB client accepts only VNC Authentication over a transport marked `TrustedSshProxy`, applies hard protocol limits before allocation, and exposes typed session events to a non-blocking egui shell. Native sessions stream directly over SSH pipes; only the explicit TigerVNC fallback creates a one-client loopback listener.

**Tech Stack:** Rust 1.92.0, Cargo, Tokio, egui/eframe, clap, serde/serde_json, secrecy/zeroize, tempfile, flate2, image, DES challenge-response, system OpenSSH, TigerVNC fallback, cargo-fuzz, cargo-audit, cargo-deny, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-29-rustedoutclient-proxmox-vnc-design.md`

## Global Constraints

- Product, app, target repository, package, and native binary names are `RustedOutClient`, `RustedOutClient`, `adamgell/RustedOutClient`, `rustedoutclient`, and `rustedoutclient`.
- Fork base is exactly `hkder/ironvnc@999e00e3a3672efdbf8e8f307e7bd60875dee67e`; preserve upstream Git history and MIT OR Apache-2.0 license files.
- Initial supported target is `aarch64-apple-darwin` on macOS 26 with Rust 1.92.0.
- `/usr/bin/ssh` is the only initial SSH implementation; strict known-hosts verification and key/agent authentication are mandatory.
- Remote commands are limited to validated `pvesh get /nodes/<node>/qemu --output-format json` and `exec /usr/sbin/qm vncproxy <vmid>` forms.
- Native RFB accepts only security type 2 over `TrustedSshProxy`; None, RA2, RA2NE, VeNCrypt, ARD, direct TCP, and unknown modes are rejected.
- No password, VNC ticket, private key, environment snapshot, clipboard content, guest pixel data, or host fingerprint is persisted or logged.
- Native sessions bind no TCP listener; TigerVNC fallback binds one listener on `127.0.0.1` only.
- Configuration/cache directories are mode 0700; files are atomically written with mode 0600.
- Protocol limits are exact: 8192 per dimension, 33,554,432 pixels, 134,217,728 framebuffer bytes, 65,536-byte names/reasons, 4,096 rectangles/update, 67,108,864 encoded bytes/rectangle, 1,048,576 clipboard bytes, 65,536 captured stderr bytes, and 256 queued UI events/session.
- Existing `/Users/Adam.Gell/.local/bin/pve-vnc`, its configuration, Desktop launcher, and TigerVNC installation remain unchanged through acceptance.
- SFTP, SCP, file transfer, VM power, snapshots, migration, storage, network management, LXC, SPICE, noVNC, and RDP are outside this plan.

## Target File Map

The implementation fork uses these responsibility boundaries:

```text
Cargo.toml                         dependency and binary surface
rust-toolchain.toml               exact Rust 1.92.0 pin
deny.toml                         dependency/license/source policy
src/lib.rs                        reusable product modules and test surface
src/main.rs                       process startup only
src/cli.rs                        GUI/list/open/probe command contract
src/config.rs                     schema-1 validation, migration, atomic writes
src/model.rs                      validated node, target, VMID, inventory types
src/diagnostics.rs                typed redacted diagnostics
src/runtime.rs                    mode-0700 short runtime directory ownership
src/ssh/command.rs                shell-free OpenSSH argv construction
src/ssh/error.rs                  OpenSSH failure classification
src/ssh/master.rs                 owned ControlMaster lifecycle
src/ssh/inventory.rs              pvesh inventory parsing and refresh
src/ssh/proxy.rs                  ticket and qm vncproxy child lifecycle
src/ssh/stream.rs                 AsyncRead/AsyncWrite over child stdout/stdin
src/cache.rs                      non-secret inventory/favorites cache
src/vnc/client.rs                 RFB state machine over trusted byte stream
src/vnc/wire.rs                   bounded wire reads and checked lengths
src/vnc/security.rs               VNC Auth-only negotiation
src/vnc/limits.rs                 exact protocol limits
src/vnc/input.rs                  tracked keys, CAD, release-all, clipboard gate
src/vnc/framebuffer.rs             bounded RGBA framebuffer
src/vnc/encoding/*.rs             checked supported decoders
src/session/model.rs              active-session state machine
src/session/manager.rs            preloading, refresh, open/reconnect/close
src/session/events.rs             bounded controller-to-UI messages
src/app/mod.rs                    egui application wiring
src/app/view.rs                   inventory, tabs, framebuffer, status rendering
src/app/actions.rs                action availability and command dispatch
src/app/state.rs                  UI-owned non-secret state
src/fallback/mod.rs               explicit TigerVNC workflow
src/fallback/password_file.rs     mode-0600 obfuscated ticket file
src/fallback/relay.rs             loopback-to-SSH single-client relay
tests/                            policy, transport, protocol, and app contracts
fuzz/                             libFuzzer targets and seed corpus
docs/                             threat model, configuration, upstream, acceptance
```

---

### Task 1: Create the fork and establish a narrow secure baseline

**Files:**
- Create in the fork: `rust-toolchain.toml`
- Create in the fork: `NOTICE`
- Create in the fork: `tests/surface_policy.rs`
- Create in the fork: `src/lib.rs`
- Modify in the fork: `Cargo.toml`
- Modify in the fork: `Cargo.lock`
- Modify in the fork: `README.md`
- Modify in the fork: `src/main.rs`
- Modify in the fork: `src/app.rs`
- Modify in the fork: `src/protocol/mod.rs`
- Modify in the fork: `src/protocol/security.rs`
- Delete in the fork: `src/transfer.rs`
- Delete in the fork: `src/sessions.rs`
- Delete in the fork: `src/protocol/ra2.rs`

**Interfaces:**
- Consumes: upstream commit `999e00e3a3672efdbf8e8f307e7bd60875dee67e`.
- Produces: buildable `rustedoutclient` package with no SFTP, saved-password, RA2, or direct-password CLI surface; remote `upstream` points to `hkder/ironvnc`.

- [ ] **Step 1: Verify the upstream commit and create the named fork**

Run:

```bash
gh api repos/hkder/ironvnc/commits/main --jq .sha
gh repo fork hkder/ironvnc --fork-name RustedOutClient --clone=false
gh repo clone adamgell/RustedOutClient /Users/Adam.Gell/repo/RustedOutClient
git -C /Users/Adam.Gell/repo/RustedOutClient remote add upstream https://github.com/hkder/ironvnc.git
git -C /Users/Adam.Gell/repo/RustedOutClient fetch upstream main
```

Expected: the first command prints `999e00e3a3672efdbf8e8f307e7bd60875dee67e`; `origin` is `adamgell/RustedOutClient`; `upstream/main` resolves to the same SHA. If upstream moved, fetch the pinned SHA and branch from the pinned SHA rather than changing this plan silently.

- [ ] **Step 2: Create the implementation branch using an isolated worktree**

Invoke `superpowers:using-git-worktrees`, then create branch `feature/proxmox-console-foundation` from the pinned commit. Record the resulting clean worktree path as the working directory for every remaining task.

Run in that worktree:

```bash
git rev-parse HEAD
git status --short --branch
```

Expected: HEAD is the pinned SHA and status is clean.

- [ ] **Step 3: Write the failing surface-policy test**

Create `tests/surface_policy.rs`:

```rust
use std::{fs, path::Path};

#[test]
fn product_surface_excludes_removed_features_and_password_cli() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"));
    let manifest = fs::read_to_string(root.join("Cargo.toml")).unwrap();
    let main = fs::read_to_string(root.join("src/main.rs")).unwrap();

    for forbidden in ["russh-sftp", "rfd =", "eax =", "aes =", "rsa ="] {
        assert!(!manifest.contains(forbidden), "forbidden dependency: {forbidden}");
    }
    for removed in ["src/transfer.rs", "src/sessions.rs", "src/protocol/ra2.rs"] {
        assert!(!root.join(removed).exists(), "removed source remains: {removed}");
    }
    for forbidden in ["--password", "sftp_test", "mod transfer", "mod sessions"] {
        assert!(!main.contains(forbidden), "forbidden CLI/source surface: {forbidden}");
    }
}
```

- [ ] **Step 4: Run RED**

Run:

```bash
cargo test --test surface_policy
```

Expected: FAIL because the upstream manifest and source still contain the forbidden surfaces.

- [ ] **Step 5: Rename the package and remove the forbidden surfaces**

Set package and binary name to `rustedoutclient`, repository to `https://github.com/adamgell/RustedOutClient`, and keep `license = "MIT OR Apache-2.0"`. Add:

```toml
[workspace.lints.rust]
unsafe_code = "forbid"

[lints]
workspace = true
```

Remove SFTP dependencies and source, password/session persistence, SFTP CLI/test modes, and RA2 dependencies/modules. Replace the upstream direct-connect startup with a minimal `RustedOutClient` window that contains no connection form and reports “Proxmox profile not configured.” Create `src/lib.rs` to export product modules and make `src/main.rs` a thin binary entrypoint. Keep the supported encoding source compiled so subsequent tasks can harden it.

Create `rust-toolchain.toml`:

```toml
[toolchain]
channel = "1.92.0"
components = ["clippy", "rustfmt"]
profile = "minimal"
```

Create `NOTICE` identifying the fork base SHA and preserving upstream attribution.

- [ ] **Step 6: Run GREEN and baseline gates**

Run:

```bash
cargo fmt --all
cargo test --test surface_policy
cargo test --all-targets
cargo clippy --all-targets -- -D warnings
```

Expected: all commands pass; `rg -n 'russh-sftp|sftp_test|sessions.json|SECURITY_RA2|--password' Cargo.toml src tests` returns no product-code match.

- [ ] **Step 7: Commit the secure fork baseline**

```bash
git add Cargo.toml Cargo.lock rust-toolchain.toml NOTICE README.md src tests/surface_policy.rs
git commit -m "chore: establish RustedOutClient secure baseline"
```

### Task 2: Add validated configuration, migration, and compatibility CLI contracts

**Files:**
- Create: `src/cli.rs`
- Create: `src/config.rs`
- Create: `src/model.rs`
- Create: `tests/config_contract.rs`
- Create: `tests/cli_contract.rs`
- Modify: `src/main.rs`
- Modify: `Cargo.toml`
- Modify: `Cargo.lock`

**Interfaces:**
- Consumes: no secret input; optional read-only legacy `~/.config/pve-vnc/config.json`.
- Produces: `AppConfig`, `PveProfile`, `VmId`, `NodeName`, `SshTarget`, `Cli`, `Command::{List,Open,Probe}`, and `ViewerMode::{Native,TigerVnc}`.

- [ ] **Step 1: Write failing validation and migration tests**

Create tests that construct only dummy values:

```rust
#[test]
fn schema_rejects_shell_like_or_secret_bearing_values() {
    assert!(SshTarget::parse("-oProxyCommand=bad").is_err());
    assert!(SshTarget::parse("root@example.invalid extra").is_err());
    assert!(NodeName::parse("pve2;id").is_err());
    assert!(VmId::new(99).is_err());
    assert!(VmId::new(100_000_000).is_err());
}

#[test]
fn legacy_import_contains_only_non_secret_fields() {
    let imported = import_legacy_json(
        r#"{"ssh_target":"root@example.invalid","node":"pve2","viewer":"/opt/homebrew/bin/vncviewer","password":"must-not-import"}"#,
    )
    .unwrap();
    let json = serde_json::to_value(imported).unwrap();
    assert!(json.get("password").is_none());
    assert_eq!(json["profile"]["node"], "pve2");
}
```

Also test refresh values 4 and 301 are rejected, relative viewer paths are rejected, configuration defaults clipboard to false, and a failed atomic rename leaves the previous valid file readable.

- [ ] **Step 2: Write failing CLI tests**

```rust
#[test]
fn cli_has_compatibility_commands_without_endpoint_or_secret_flags() {
    assert!(Cli::try_parse_from(["rustedoutclient", "list"]).is_ok());
    assert!(Cli::try_parse_from(["rustedoutclient", "open", "labz1-cm01", "--view-only"]).is_ok());
    assert!(Cli::try_parse_from(["rustedoutclient", "probe", "107", "--json"]).is_ok());
    assert!(Cli::try_parse_from(["rustedoutclient", "--password", "bad"]).is_err());
    assert!(Cli::try_parse_from(["rustedoutclient", "--host", "example.invalid"]).is_err());
}
```

- [ ] **Step 3: Run RED**

Run:

```bash
cargo test --test config_contract --test cli_contract
```

Expected: compile failure because the modules and types do not exist.

- [ ] **Step 4: Implement exact model and configuration types**

Define:

```rust
pub struct SshTarget(String);
pub struct NodeName(String);
pub struct VmId(u32);

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct PveProfile {
    pub name: String,
    pub ssh_target: SshTarget,
    pub node: NodeName,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct AppConfig {
    pub schema_version: u32,
    pub profile: PveProfile,
    pub inventory_refresh_seconds: u64,
    pub fallback_viewer: Option<PathBuf>,
    pub clipboard_enabled: bool,
    pub favorites: Vec<FavoriteVm>,
    pub display: DisplayPreferences,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct FavoriteVm {
    pub vmid: VmId,
    pub alias: Option<String>,
    pub scale_mode: ScaleMode,
    pub view_only: bool,
    pub sort_position: u32,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct DisplayPreferences {
    pub scale_mode: ScaleMode,
    pub view_only: bool,
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ScaleMode {
    Fit,
    OneToOne,
}
```

Implement the exact validation and mode requirements from the spec. Configuration lives under `~/Library/Application Support/RustedOutClient`. Atomic save writes a temporary file in that directory, sets mode 0600 before content, calls `sync_all`, renames, and syncs the parent directory. Do not derive or implement `Debug` for a future secret type.

- [ ] **Step 5: Implement the CLI grammar**

Define default GUI launch and these subcommands:

```rust
pub enum Command {
    List,
    Open {
        selector: String,
        fullscreen: bool,
        view_only: bool,
        viewer: ViewerMode,
    },
    Probe {
        selector: String,
        timeout_seconds: u64,
        json: bool,
    },
}

pub enum ViewerMode {
    Native,
    TigerVnc,
}
```

`Probe` defaults to 30 seconds and reports first-frame timing without accepting a ticket or password argument. `Open` defaults to native.

- [ ] **Step 6: Run GREEN and commit**

Run:

```bash
cargo fmt --all
cargo test --test config_contract --test cli_contract
cargo clippy --all-targets -- -D warnings
```

Expected: PASS.

```bash
git add Cargo.toml Cargo.lock src/main.rs src/cli.rs src/config.rs src/model.rs tests/config_contract.rs tests/cli_contract.rs
git commit -m "feat: define secure configuration and CLI contracts"
```

### Task 3: Build shell-free strict OpenSSH command contracts

**Files:**
- Create: `src/ssh/mod.rs`
- Create: `src/ssh/command.rs`
- Create: `src/ssh/error.rs`
- Create: `tests/ssh_command_contract.rs`
- Modify: `src/main.rs`
- Modify: `Cargo.toml`
- Modify: `Cargo.lock`

**Interfaces:**
- Consumes: validated `PveProfile`, `NodeName`, `VmId`, and an internal SSH executable path.
- Produces: `SshCommandFactory::{master,check,exit,inventory,proxy}` returning `CommandSpec`, plus `SshFailureKind`.

- [ ] **Step 1: Write failing exact-argv tests**

```rust
#[test]
fn inventory_argv_is_strict_and_shell_free() {
    let factory = fixture_factory();
    let spec = factory.inventory(&fixture_profile()).unwrap();
    assert_eq!(spec.program, PathBuf::from("/usr/bin/ssh"));
    assert!(spec.args.windows(2).any(|w| w == ["-o", "StrictHostKeyChecking=yes"]));
    assert!(spec.args.windows(2).any(|w| w == ["-o", "PasswordAuthentication=no"]));
    assert_eq!(spec.args.last().unwrap(), "pvesh get /nodes/pve2/qemu --output-format json");
    assert!(!spec.args.iter().any(|a| a == "sh" || a == "zsh" || a == "-c"));
}

#[test]
fn proxy_remote_command_contains_only_decimal_vmid() {
    let spec = fixture_factory().proxy(&fixture_profile(), VmId::new(107).unwrap()).unwrap();
    assert_eq!(spec.args.last().unwrap(), "exec /usr/sbin/qm vncproxy 107");
}
```

Test that every operation includes BatchMode, ConnectTimeout, ServerAliveInterval, ServerAliveCountMax, StrictHostKeyChecking, PasswordAuthentication, and KbdInteractiveAuthentication. Both authentication options equal `no`. Master additionally has `-M -N`, `ControlPersist=no`, and a control socket; child operations use `-S` with that socket.

- [ ] **Step 2: Write failure-classification tests**

Use bounded dummy stderr strings to assert host-key unknown, changed key, authentication, timeout, and generic SSH errors become distinct `SshFailureKind` values. Assert the public display text omits the SSH target.

- [ ] **Step 3: Run RED**

Run:

```bash
cargo test --test ssh_command_contract
```

Expected: compile failure because `src/ssh` does not exist.

- [ ] **Step 4: Implement immutable command specs**

Define:

```rust
pub struct CommandSpec {
    pub program: PathBuf,
    pub args: Vec<OsString>,
    pub env: Vec<(OsString, SecretString)>,
    pub capture_stderr: bool,
}
```

Production construction hardcodes `/usr/bin/ssh`; tests may inject a test-owned executable. Build `std::process::Command` or `tokio::process::Command` directly from `CommandSpec`. Do not construct one joined command string and do not invoke a shell.

Add `secrecy = "0.10"`; `CommandSpec` deliberately has no derived `Debug` or serialization implementation.

Implement bounded stderr capture and redacted error classification. Never include `CommandSpec.env`, target text, raw stderr, or fingerprints in `Debug` or display output.

- [ ] **Step 5: Run GREEN and commit**

Run:

```bash
cargo fmt --all
cargo test --test ssh_command_contract
cargo clippy --all-targets -- -D warnings
```

Expected: PASS.

```bash
git add src/main.rs src/ssh tests/ssh_command_contract.rs
git commit -m "feat: enforce strict OpenSSH command construction"
```

### Task 4: Add the owned ControlMaster, live inventory, and non-secret cache

**Files:**
- Create: `src/runtime.rs`
- Create: `src/cache.rs`
- Create: `src/ssh/master.rs`
- Create: `src/ssh/inventory.rs`
- Create: `tests/support/fake_ssh.sh`
- Create: `tests/ssh_inventory_contract.rs`
- Modify: `src/ssh/mod.rs`
- Modify: `Cargo.toml`
- Modify: `Cargo.lock`

**Interfaces:**
- Consumes: `SshCommandFactory`, validated profile, refresh interval, and schema-1 cache path.
- Produces: `RuntimeDir`, `SshMaster::{start,check,close}`, `InventoryClient::fetch`, `InventorySnapshot`, and `InventoryCache::{load,save}`.

- [ ] **Step 1: Write failing runtime and master tests**

```rust
#[tokio::test]
async fn master_uses_private_short_runtime_path_and_exits_owned_child() {
    let runtime = RuntimeDir::create().unwrap();
    assert!(runtime.control_socket().starts_with("/tmp/roc-"));
    assert_eq!(mode(runtime.path()), 0o700);

    let mut master = SshMaster::start(fixture_factory(runtime.control_socket()), fixture_profile())
        .await
        .unwrap();
    master.check().await.unwrap();
    master.close().await.unwrap();
    assert!(!master.is_running());
}
```

The fake SSH fixture records only argv and emits no environment. Make it executable in test setup with mode 0700.

- [ ] **Step 2: Write failing inventory/cache tests**

Fixture JSON must include running and stopped VMs, names, VMIDs, status, node, and a template entry. Assert sorting by VMID, exact/case-insensitive selector rules, duplicate-name ambiguity, stopped-state preservation, and template filtering.

Assert cached snapshots include `observed_at_unix_ms` and `stale=true` after load, contain no `ssh_target`, and are atomically written with mode 0600.

- [ ] **Step 3: Run RED**

Run:

```bash
cargo test --test ssh_inventory_contract
```

Expected: compile failure because runtime, master, inventory, and cache types do not exist.

- [ ] **Step 4: Implement the runtime directory and ControlMaster lifecycle**

Create a random `/tmp/roc-<random>` directory with mode 0700 and a short `c` control-socket path. Hold its `TempDir` for the application lifetime. `SshMaster::start` owns the child; `check` uses `ssh -S <socket> -O check`; `close` requests `-O exit`, waits three seconds, then calls `start_kill` only on the owned child.

Do not scan `/tmp`, inspect unrelated PIDs, or kill by process name.

- [ ] **Step 5: Implement bounded inventory parsing and selection**

Define:

```rust
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct VmInventoryItem {
    pub vmid: VmId,
    pub name: String,
    pub node: NodeName,
    pub status: VmStatus,
    pub template: bool,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct InventorySnapshot {
    pub observed_at_unix_ms: u64,
    pub stale: bool,
    pub vms: Vec<VmInventoryItem>,
}
```

Cap inventory stdout at 4 MiB before JSON parsing. Reject malformed records rather than inventing VMIDs or statuses. Filter templates from the openable list while preserving them only if diagnostics needs to report their exclusion.

- [ ] **Step 6: Run GREEN and commit**

Run:

```bash
cargo fmt --all
cargo test --test ssh_inventory_contract
cargo clippy --all-targets -- -D warnings
```

Expected: PASS and no fake-SSH child remains after the test process.

```bash
git add Cargo.toml Cargo.lock src/runtime.rs src/cache.rs src/ssh tests/support/fake_ssh.sh tests/ssh_inventory_contract.rs
git commit -m "feat: preload verified SSH inventory"
```

### Task 5: Create ephemeral proxy tickets and a native SSH byte stream

**Files:**
- Create: `src/ssh/proxy.rs`
- Create: `src/ssh/stream.rs`
- Create: `tests/proxy_transport_contract.rs`
- Modify: `src/ssh/mod.rs`
- Modify: `src/ssh/command.rs`
- Modify: `Cargo.toml`
- Modify: `Cargo.lock`

**Interfaces:**
- Consumes: verified `SshMaster`, validated running `VmInventoryItem`, and fixed proxy command.
- Produces: `ProxyTicket`, `TrustedSshProxy`, and `ProxyStream: AsyncRead + AsyncWrite` with owned child cleanup.

- [ ] **Step 1: Write failing ticket tests**

```rust
#[test]
fn tickets_are_eight_ascii_alphanumeric_characters_and_not_debuggable() {
    let a = ProxyTicket::generate();
    let b = ProxyTicket::generate();
    assert_eq!(a.expose_for_auth().len(), 8);
    assert!(a.expose_for_auth().bytes().all(|c| c.is_ascii_alphanumeric()));
    assert_ne!(a.expose_for_auth(), b.expose_for_auth());
    assert_eq!(format!("{a:?}"), "ProxyTicket([REDACTED])");
}
```

Generate 10,000 tickets in the test and assert every value satisfies shape requirements and at least 9,990 are unique; do not print collisions or values.

- [ ] **Step 2: Write failing argv/environment and stream tests**

Use a fake executable that records argv, verifies `LC_PVE_TICKET` exists without writing its value, emits `RFB 003.008\n` on stdout, and reads one byte from stdin. Assert:

```rust
assert!(!recorded_argv.contains("LC_PVE_TICKET"));
assert!(!recorded_argv.contains(ticket.expose_for_auth()));
assert_eq!(&banner, b"RFB 003.008\n");
```

Also assert the proxy argv contains `-o SendEnv=LC_PVE_TICKET` exactly once and every non-proxy SSH operation omits it.

Close the stream and assert the owned child exits and no ticket-bearing file exists in the runtime directory.

- [ ] **Step 3: Run RED**

Run:

```bash
cargo test --test proxy_transport_contract
```

Expected: compile failure because the proxy types do not exist.

- [ ] **Step 4: Implement secret and child ownership**

Use `secrecy::SecretString`, `zeroize`, `rand::rngs::OsRng`, and `rand::distributions::Alphanumeric`. `ProxyTicket` exposes its inner value only to the command factory and VNC authentication call. It implements a fixed redacted `Debug` and no serialization trait.

`TrustedSshProxy` is constructible only inside `ssh::proxy` after a verified master is active and a fresh live inventory item is confirmed running.

- [ ] **Step 5: Implement direct RFB streaming over SSH pipes**

`ProxyStream` owns `tokio::process::Child`, `ChildStdout`, and `ChildStdin`. Implement `AsyncRead` by delegating to stdout and `AsyncWrite` by delegating to stdin. `close()` shuts down stdin, waits three seconds, and kills only its child if needed. Native code receives the stream directly and opens no `TcpListener`.

- [ ] **Step 6: Run GREEN, scan for exposure, and commit**

Run:

```bash
cargo fmt --all
cargo test --test proxy_transport_contract
cargo clippy --all-targets -- -D warnings
rg -n 'expose_for_auth|LC_PVE_TICKET' src
```

Expected: tests pass; exposure calls exist only in `src/ssh/proxy.rs`, `src/ssh/command.rs`, and the VNC authentication boundary added in Task 6.

```bash
git add Cargo.toml Cargo.lock src/ssh tests/proxy_transport_contract.rs
git commit -m "feat: stream ephemeral Proxmox VNC proxies over SSH"
```

### Task 6: Refactor the RFB core around a bounded trusted-stream API

**Files:**
- Create: `src/vnc/mod.rs`
- Create: `src/vnc/client.rs`
- Create: `src/vnc/wire.rs`
- Create: `src/vnc/limits.rs`
- Create: `src/vnc/security.rs`
- Create: `tests/rfb_handshake_contract.rs`
- Create: `tests/ui/untrusted_stream.rs`
- Create: `tests/ui/untrusted_stream.stderr`
- Move: `src/protocol/messages.rs` to `src/vnc/messages.rs`
- Modify: `src/connection.rs`
- Modify: `src/main.rs`
- Delete after migration: `src/protocol/client.rs`
- Delete after migration: `src/protocol/security.rs`
- Delete after migration: `src/protocol/mod.rs`
- Modify: `Cargo.toml`
- Modify: `Cargo.lock`

**Interfaces:**
- Consumes: `TrustedSshProxy`, `ProxyStream`, and `ProxyTicket` from Task 5.
- Produces: `VncClient::run(TrustedSshProxy, VncOptions, Sender<VncEvent>, Receiver<VncCommand>)`, `ProtocolLimits`, `RfbReader`, and VNC Auth-only security negotiation.

- [ ] **Step 1: Write failing version and allowlist tests**

Use `tokio::io::duplex` and fixture byte streams:

```rust
#[tokio::test]
async fn malformed_banner_is_rejected_instead_of_downgraded() {
    let err = negotiate_fixture(b"NOT RFB DATA", &[2]).await.unwrap_err();
    assert_eq!(err.kind(), RfbErrorKind::ProtocolBanner);
}

#[tokio::test]
async fn only_vnc_auth_is_accepted_over_trusted_ssh() {
    assert!(negotiate_fixture(b"RFB 003.008\n", &[2]).await.is_ok());
    for offered in [[1].as_slice(), [5].as_slice(), [6].as_slice(), [19].as_slice(), [129].as_slice()] {
        let err = negotiate_fixture(b"RFB 003.008\n", offered).await.unwrap_err();
        assert_eq!(err.kind(), RfbErrorKind::SecurityAllowlist);
    }
}
```

Add RFB 3.3 tests where the server's dictated `u32` must equal 2. Test 3.7, 3.8, and a valid higher version capped at 3.8. Test that raw `ProxyStream` cannot call the public VNC API without a `TrustedSshProxy` wrapper at compile time using `trybuild`.

Add `trybuild = "1"` under dev-dependencies for the compile-fail boundary test.

- [ ] **Step 2: Write failing bounded-string tests**

Assert desktop-name and failure-reason lengths 65,536 are accepted, 65,537 are rejected before allocation, truncated values produce `UnexpectedEof`, and no parser uses `.min(limit)` to continue a declared oversized field.

- [ ] **Step 3: Run RED**

Run:

```bash
cargo test --test rfb_handshake_contract
```

Expected: compile failure because the trusted-stream client and error types do not exist.

- [ ] **Step 4: Define exact limits and bounded wire helpers**

Create:

```rust
#[derive(Clone, Copy, Debug)]
pub struct ProtocolLimits {
    pub max_dimension: u16,
    pub max_pixels: u64,
    pub max_framebuffer_bytes: u64,
    pub max_text_bytes: u32,
    pub max_rectangles: u16,
    pub max_encoded_rect_bytes: u32,
    pub max_clipboard_bytes: u32,
}

impl Default for ProtocolLimits {
    fn default() -> Self {
        Self {
            max_dimension: 8_192,
            max_pixels: 33_554_432,
            max_framebuffer_bytes: 134_217_728,
            max_text_bytes: 65_536,
            max_rectangles: 4_096,
            max_encoded_rect_bytes: 67_108_864,
            max_clipboard_bytes: 1_048_576,
        }
    }
}
```

`RfbReader::read_bounded_bytes(declared, limit, field)` checks `declared <= limit` before creating a `Vec`. It returns an `RfbLimit` error and closes the session on violation.

- [ ] **Step 5: Refactor connection startup to consume only trusted SSH**

Remove every `TcpStream::connect`, host, port, and password field from the VNC modules. `VncClient::run` takes ownership of `TrustedSshProxy`, splits out the stream and ticket, negotiates type 2, zeroizes the ticket after writing the challenge response, and then enters ServerInit.

Use a bounded sync channel with capacity 256 for events and a bounded channel with capacity 256 for commands. Framebuffer updates that would overflow the event queue coalesce dirty rectangles rather than growing memory without bound; state/error events are never dropped.

- [ ] **Step 6: Implement strict banner and security negotiation**

Accept only exact ASCII `RFB ddd.ddd\n` framing. For 3.7/3.8, choose type 2 only if offered. For 3.3, require dictated type 2. Reject zero security types with a bounded reason. Do not compile any RA2 branch or type-None success branch.

- [ ] **Step 7: Run GREEN and commit**

Run:

```bash
cargo fmt --all
cargo test --test rfb_handshake_contract
cargo test --all-targets
cargo clippy --all-targets -- -D warnings
rg -n 'TcpStream::connect|SECURITY_NONE|SECURITY_RA2|RA2NE' src
```

Expected: all gates pass and the final search returns no product-code match.

```bash
git add Cargo.toml Cargo.lock src tests/rfb_handshake_contract.rs
git commit -m "feat: require bounded VNC auth over trusted SSH"
```

### Task 7: Harden framebuffer and supported encoding decoders

**Files:**
- Move: `src/framebuffer.rs` to `src/vnc/framebuffer.rs`
- Move: `src/protocol/encoding/raw.rs` to `src/vnc/encoding/raw.rs`
- Move: `src/protocol/encoding/copyrect.rs` to `src/vnc/encoding/copyrect.rs`
- Move: `src/protocol/encoding/hextile.rs` to `src/vnc/encoding/hextile.rs`
- Move: `src/protocol/encoding/zrle.rs` to `src/vnc/encoding/zrle.rs`
- Move: `src/protocol/encoding/tight.rs` to `src/vnc/encoding/tight.rs`
- Create: `src/vnc/encoding/mod.rs`
- Create: `tests/framebuffer_limits.rs`
- Create: `tests/encoding_contract.rs`
- Modify: `src/vnc/client.rs`
- Modify: `src/vnc/messages.rs`

**Interfaces:**
- Consumes: `ProtocolLimits`, bounded reader, `PixelFormat`, and rectangle headers.
- Produces: `CheckedRect`, bounded `Framebuffer`, and checked decoders for Raw, CopyRect, Hextile, ZRLE, Tight, DesktopSize, and cursor.

- [ ] **Step 1: Write failing framebuffer dimension and arithmetic tests**

```rust
#[test]
fn framebuffer_rejects_dimension_pixel_and_byte_overflow() {
    let limits = ProtocolLimits::default();
    assert!(Framebuffer::new(8_192, 4_096, limits).is_ok());
    assert!(Framebuffer::new(8_193, 1, limits).is_err());
    assert!(Framebuffer::new(8_192, 8_192, limits).is_err());
    assert!(CheckedRect::new(u16::MAX, u16::MAX, u16::MAX, u16::MAX, 800, 600).is_err());
}

#[test]
fn copyrect_validates_source_and_destination() {
    let fb = fixture_framebuffer(800, 600);
    assert!(copy_rect(&fb, rect(0, 0, 20, 20), 790, 590).is_err());
    assert!(copy_rect(&fb, rect(790, 590, 20, 20), 0, 0).is_err());
}
```

- [ ] **Step 2: Write malicious encoding tests**

For each decoder, include valid minimal data plus these failures:

- Raw: checked `width * height * bytes_per_pixel` overflow and truncation.
- Hextile: subrect outside tile, subrect count exhausting payload, and invalid background/foreground state.
- ZRLE: compressed length 67,108,865, palette index outside palette, run exceeding tile pixels, and decompressor with no progress.
- Tight: compact length 67,108,865, invalid stream reset bits, palette count/index errors, JPEG dimensions different from rectangle, and decompressed output exceeding rectangle bytes.
- DesktopSize/cursor: oversized allocation, invalid cursor payload length, and rectangle outside framebuffer.

Every test asserts a typed error and unchanged sentinel bytes around the destination region.

- [ ] **Step 3: Run RED**

Run:

```bash
cargo test --test framebuffer_limits --test encoding_contract
```

Expected: current decoders fail at least the oversized and out-of-bounds cases.

- [ ] **Step 4: Introduce checked rectangle and framebuffer APIs**

Define `CheckedRect::new(x, y, width, height, fb_width, fb_height)` using `checked_add`. Define expected byte counts with `checked_mul` and convert to `usize` only after comparing against limits. Remove any direct framebuffer index arithmetic from decoders; all writes go through checked row or region methods.

- [ ] **Step 5: Harden each decoder independently**

Apply declared-length checks before allocation, cap decompression by exact rectangle output, reject no-progress loops, validate every palette/run/tile coordinate, and check JPEG dimensions before copying pixels. Unknown encodings close the session without attempting a skip.

- [ ] **Step 6: Run focused tests after each decoder, then the full gate**

Run:

```bash
cargo test --test framebuffer_limits
cargo test --test encoding_contract raw
cargo test --test encoding_contract copyrect
cargo test --test encoding_contract hextile
cargo test --test encoding_contract zrle
cargo test --test encoding_contract tight
cargo test --all-targets
cargo clippy --all-targets -- -D warnings
```

Expected: every command passes without panic or excessive allocation.

- [ ] **Step 7: Commit hardened decoders**

```bash
git add src/vnc tests/framebuffer_limits.rs tests/encoding_contract.rs
git commit -m "fix: bound RFB framebuffer decoders"
```

### Task 8: Add parser fuzzing and committed seed corpora

**Files:**
- Create: `fuzz/Cargo.toml`
- Create: `fuzz/fuzz_targets/rfb_banner_security.rs`
- Create: `fuzz/fuzz_targets/server_init.rs`
- Create: `fuzz/fuzz_targets/framebuffer_update.rs`
- Create: `fuzz/fuzz_targets/encoding_payload.rs`
- Create: `fuzz/fuzz_targets/server_clipboard.rs`
- Create: `fuzz/corpus/rfb_banner_security/*`
- Create: `fuzz/corpus/server_init/*`
- Create: `fuzz/corpus/framebuffer_update/*`
- Create: `fuzz/corpus/encoding_payload/*`
- Create: `fuzz/corpus/server_clipboard/*`
- Create: `scripts/fuzz-smoke.sh`
- Modify: `.gitignore`
- Modify: `src/vnc/mod.rs`

**Interfaces:**
- Consumes: pure bounded parser/decoder entry points from Tasks 6-7.
- Produces: five libFuzzer targets whose invariant is no panic, abort, unbounded allocation, infinite loop, or out-of-bounds write.

- [ ] **Step 1: Expose fuzz-safe pure entry points**

Add non-network functions that accept `&[u8]`, `ProtocolLimits`, and a bounded test framebuffer. Keep them behind a public `fuzzing` module enabled only by the `fuzzing` Cargo feature; do not weaken production validation.

The nested `fuzz/Cargo.toml` contains an empty `[workspace]` table so it remains isolated from the product workspace, and pins `libfuzzer-sys = "0.4"`.

- [ ] **Step 2: Write the fuzz targets**

Each target follows this exact shape:

```rust
#![no_main]
use libfuzzer_sys::fuzz_target;
use rustedoutclient::vnc::{fuzzing, ProtocolLimits};

fuzz_target!(|data: &[u8]| {
    let _ = fuzzing::parse_framebuffer_update(data, ProtocolLimits::default());
});
```

The encoding target uses the first input byte to select Raw, CopyRect, Hextile, ZRLE, or Tight and always allocates a fixed 64x64 test framebuffer.

- [ ] **Step 3: Seed each corpus with valid and malformed fixtures**

Include one minimal valid frame, one truncated frame at each length boundary, one declared-over-limit length, one arithmetic-overflow shape, and one unknown type. Fixtures contain synthetic pixels and no live guest data.

- [ ] **Step 4: Run all fuzz targets for 10 seconds each**

Create `scripts/fuzz-smoke.sh` with `set -euo pipefail` and explicit target names. Run:

```bash
cargo install cargo-fuzz --version 0.13.2 --locked
./scripts/fuzz-smoke.sh 10
```

Expected: five targets complete without crash, timeout, or generated crash artifact.

- [ ] **Step 5: Minimize corpora and commit**

Run `cargo fuzz cmin` for each target, rerun the smoke script, then:

```bash
git add .gitignore src/vnc fuzz scripts/fuzz-smoke.sh
git commit -m "test: fuzz bounded RFB parsing"
```

### Task 9: Build the session manager, startup preloader, and reconnect lifecycle

**Files:**
- Create: `src/session/mod.rs`
- Create: `src/session/model.rs`
- Create: `src/session/events.rs`
- Create: `src/session/manager.rs`
- Create: `tests/session_manager_contract.rs`
- Modify: `src/main.rs`
- Modify: `src/cache.rs`
- Modify: `src/ssh/inventory.rs`
- Modify: `src/ssh/proxy.rs`
- Modify: `src/vnc/client.rs`
- Modify: `Cargo.toml`
- Modify: `Cargo.lock`

**Interfaces:**
- Consumes: configuration, cached/live inventory, SSH master, proxy stream, and VNC events.
- Produces: `SessionManager`, `AppEvent`, `AppCommand`, `SessionId`, and deterministic `SessionPhase` transitions.

- [ ] **Step 1: Write failing state-machine tests**

Define and assert the allowed path:

```rust
assert_path(&[
    SessionPhase::Opening,
    SessionPhase::StartingProxy,
    SessionPhase::NegotiatingRfb,
    SessionPhase::Ready,
    SessionPhase::Disconnecting,
    SessionPhase::Disconnected,
]);
```

Reject Ready before negotiation, Reconnect without cleanup, and a second active session for the same profile/VMID. Assert opening an active VM emits FocusExisting rather than spawning another proxy.

- [ ] **Step 2: Write failing preload and revalidation tests**

Start with a cached running VM, then make fresh inventory report stopped. Assert startup emits cached stale inventory immediately, the live snapshot replaces it, and Open returns `VmNotRunning` without generating a ticket. Test 15-second periodic refresh with Tokio's paused clock.

- [ ] **Step 3: Write failing cleanup tests**

Inject failures at master check, inventory, proxy spawn, RFB negotiation, first frame, and UI cancellation. Assert each owned child receives close, each session ends in one typed Error/Disconnected event, the event channel never exceeds 256 entries, and no reconnect occurs automatically after trust/auth/protocol errors.

- [ ] **Step 4: Run RED**

Run:

```bash
cargo test --test session_manager_contract
```

Expected: compile failure because session manager types do not exist.

- [ ] **Step 5: Implement bounded commands/events and startup orchestration**

Define:

```rust
pub enum AppCommand {
    RefreshInventory,
    Open { vmid: VmId, options: OpenOptions },
    Reconnect { session_id: SessionId },
    Close { session_id: SessionId },
    SendInput { session_id: SessionId, action: InputAction },
    Shutdown,
}

pub enum AppEvent {
    CachedInventory(InventorySnapshot),
    LiveInventory(InventorySnapshot),
    SessionChanged(SessionSnapshot),
    Framebuffer { session_id: SessionId, rects: Vec<FbRect> },
    Error(PublicError),
}
```

Use bounded Tokio channels of 256. Startup loads cache, emits it stale, starts/checks master, fetches live inventory, saves cache, and schedules refresh. Open always performs a fresh selected-VM validation before ticket generation. Add `uuid = { version = "1", features = ["v4"] }` and define `SessionId(uuid::Uuid)` without serialization.

- [ ] **Step 6: Implement idempotent close and reconnect**

Close releases keys, closes RFB, closes proxy, and publishes Disconnected. Reconnect waits for that cleanup and starts a new ticket/proxy. Session IDs are random non-secret UUIDs and are not persisted.

- [ ] **Step 7: Run GREEN and commit**

Run:

```bash
cargo fmt --all
cargo test --test session_manager_contract
cargo test --all-targets
cargo clippy --all-targets -- -D warnings
```

Expected: PASS.

```bash
git add src/main.rs src/cache.rs src/ssh src/vnc src/session tests/session_manager_contract.rs
git commit -m "feat: manage preloaded Proxmox console sessions"
```

### Task 10: Implement reliable keyboard, Ctrl+Alt+Delete, and bounded clipboard actions

**Files:**
- Create: `src/vnc/input.rs`
- Create: `tests/input_contract.rs`
- Modify: `src/vnc/client.rs`
- Modify: `src/session/manager.rs`
- Modify: `src/session/model.rs`

**Interfaces:**
- Consumes: egui key/pointer events and explicit session actions.
- Produces: `InputController::{key,ctrl_alt_delete,release_all_keys,set_view_only,send_clipboard}` and `InputAction`.

- [ ] **Step 1: Write failing Ctrl+Alt+Delete ordering tests**

```rust
#[test]
fn ctrl_alt_delete_has_exact_press_and_release_order() {
    let mut sink = RecordingSink::default();
    let mut input = InputController::new(&mut sink);
    input.ctrl_alt_delete().unwrap();
    assert_eq!(sink.keys, [
        (true, 0xFFE3),
        (true, 0xFFE9),
        (true, 0xFFFF),
        (false, 0xFFFF),
        (false, 0xFFE9),
        (false, 0xFFE3),
    ]);
}
```

- [ ] **Step 2: Write failing stuck-key and view-only tests**

Press Control and Alt, inject a write failure, then call release-all and assert key-up is attempted for every tracked key and the set is empty. Assert focus loss, disconnect, and enabling view-only invoke release-all. Assert CAD, pointer, key, and clipboard actions are rejected while view-only or not Ready.

- [ ] **Step 3: Write failing clipboard tests**

Assert clipboard defaults off, 1,048,576 UTF-8 bytes are accepted when enabled, 1,048,577 are rejected, invalid UTF-8 is not constructed, and text never appears in `Debug`, diagnostics, cache, or logs. Incoming text is held only until the explicit Receive action completes or the session closes.

- [ ] **Step 4: Run RED**

Run:

```bash
cargo test --test input_contract
```

Expected: compile failure because `InputController` does not exist.

- [ ] **Step 5: Implement tracked input state**

Use a `BTreeSet<u32>` of pressed keysyms. Insert before successful key-down transmission and remove after key-up attempt. `release_all_keys` sends key-up in reverse deterministic order and clears state even when the sink reports an error; return the first error after cleanup attempts.

Implement CAD as the exact six-event sequence from the spec. Do not synthesize the shortcut through macOS APIs.

- [ ] **Step 6: Implement explicit clipboard gating**

No polling loop reads the Mac clipboard. Send and Receive are explicit actions, text-only, bounded, and disabled by default. Clear all clipboard buffers on close.

- [ ] **Step 7: Run GREEN and commit**

Run:

```bash
cargo fmt --all
cargo test --test input_contract
cargo clippy --all-targets -- -D warnings
```

Expected: PASS.

```bash
git add src/vnc/input.rs src/vnc/client.rs src/session tests/input_contract.rs
git commit -m "feat: add reliable secure-attention input controls"
```

### Task 11: Replace the monolithic upstream UI with the RustedOutClient session shell

**Files:**
- Create: `src/app/mod.rs`
- Create: `src/app/state.rs`
- Create: `src/app/actions.rs`
- Create: `src/app/view.rs`
- Create: `tests/app_state_contract.rs`
- Create: `tests/display_resize_contract.rs`
- Modify: `src/main.rs`
- Modify: `src/session/events.rs`
- Modify: `src/session/model.rs`
- Modify: `src/connection.rs`
- Modify: `src/vnc/messages.rs`
- Modify: `src/vnc/client.rs`
- Delete after migration: `src/app.rs`

**Interfaces:**
- Consumes: `AppCommand`, `AppEvent`, `InventorySnapshot`, `SessionSnapshot`, and `InputAction`.
- Produces: `RustedOutClientApp`, `AppState`, `ActionAvailability`, searchable inventory/favorites, native tabs, framebuffer viewport, bounded dynamic-resolution state, Session/View menus, toolbar, and status bar.

- [ ] **Step 1: Write failing action-availability tests**

```rust
#[test]
fn unsafe_actions_require_a_ready_writable_session() {
    let disconnected = ActionAvailability::from_state(&fixture_state(SessionPhase::Disconnected, false));
    assert!(!disconnected.ctrl_alt_delete);
    assert!(!disconnected.send_clipboard);

    let view_only = ActionAvailability::from_state(&fixture_state(SessionPhase::Ready, true));
    assert!(!view_only.ctrl_alt_delete);
    assert!(!view_only.send_key_input);

    let ready = ActionAvailability::from_state(&fixture_state(SessionPhase::Ready, false));
    assert!(ready.ctrl_alt_delete);
    assert!(ready.release_all_keys);
    assert!(ready.reconnect);
}
```

Test every menu action against no session, connecting, ready, view-only, error, and disconnected states.

Dynamic Resolution is available only for a Ready native session with a valid viewport. Fit to Window and 1:1 remain available regardless of whether guest resize is supported.

- [ ] **Step 2: Write failing inventory/tab state tests**

Assert favorites sort before non-favorites, search matches alias/name/VMID case-insensitively, stale timestamps remain visible until live inventory arrives, stopped VMs cannot invoke Open, opening an active VM focuses its existing tab, and two different VMIDs produce two tabs.

Serialize or debug-print the UI state and assert it contains no field named password, ticket, clipboard_text, environment, private_key, or raw_stderr.

- [ ] **Step 3: Write failing dynamic-resolution protocol and state tests**

Advertise both DesktopSize (`-223`) and ExtendedDesktopSize (`-308`). Assert the exact one-screen SetDesktopSize (`251`) wire message for 1,600x900 and 1,920x1,080 viewports. Reject dimensions below 640x480 or above the existing 8,192-by-8,192 / 33,554,432-pixel policy before queueing.

Test a 250-ms paused-clock debounce, one request in flight, replacement by the newest desired size, and no resize storm during 1,000 rapid viewport changes. Parse valid ExtendedDesktopSize reason/result/screen payloads with checked lengths and screen counts. Treat QEMU `Request forwarded` as Pending; only a subsequent matching valid framebuffer-size update is Applied. Rejection, unsupported layout, and a two-second no-change timeout leave the session healthy, stop automatic retries, and retain Fit to Window. Malformed responses fail closed without allocating outside protocol limits.

- [ ] **Step 4: Run RED**

Run:

```bash
cargo test --test app_state_contract
cargo test --test display_resize_contract
```

Expected: compile failure because the split app model does not exist.

- [ ] **Step 5: Implement pure state reduction, action dispatch, and resize negotiation**

`AppState::apply(AppEvent)` is deterministic and side-effect free. UI actions send typed `AppCommand` values; view code never launches processes or performs network I/O. Framebuffer texture updates use dirty rectangles and reuse textures when dimensions remain unchanged.

Add a bounded semantic ResizeDisplay command. The session worker owns debounce/in-flight state and the existing VNC command queue owns the only wire path. Derive the desired size from the usable viewport's backing pixels, round down to multiples of eight, and apply the existing framebuffer dimension/pixel ceilings. Advertise/parse ExtendedDesktopSize in the production parser; do not create a second parser. A resize rejection or timeout is a typed nonterminal capability result, not an RFB disconnect.

- [ ] **Step 6: Implement the window layout and menus**

Render:

```text
Menu bar: RustedOutClient | Session | View | Help
Sidebar: search, favorites, live/stale status, VMID, VM name
Tabs: one per active native VM
Viewport: framebuffer with fit or 1:1 scale
Toolbar: Reconnect | CAD | Release Keys | View Only | Dynamic Resolution | Fit | 1:1 | Fullscreen | TigerVNC
Status: profile | node | VM | inventory age | session phase | guest WxH | resize state | clipboard state
```

Session menu labels are exactly Open, Reconnect, Close, Open in TigerVNC, Ctrl+Alt+Delete, Release All Keys, View Only, Fit to Window, 1:1, Fullscreen, Send Clipboard, Receive Clipboard, and Diagnostics.

Place Dynamic Resolution in the View menu and toolbar. Clearly distinguish an Applied guest resize from local Fit scaling. Rejected, Unsupported, and Timed out states remain visible and offer a manual Retry without changing VM hardware or guest configuration.

- [ ] **Step 7: Preserve responsiveness under worker delay**

Add a test worker that delays inventory and framebuffer events. Drive 1,000 UI updates and assert `AppState::apply` never blocks on a worker channel send. Commands use non-blocking `try_send`; a full queue surfaces a typed Busy error rather than freezing egui.

- [ ] **Step 8: Run GREEN and launch local shell**

Run:

```bash
cargo fmt --all
cargo test --test app_state_contract
cargo test --test display_resize_contract
cargo test --all-targets
cargo clippy --all-targets -- -D warnings
cargo run --release
```

Expected: automated gates pass; the app opens as RustedOutClient, shows cached/live inventory states when configured, contains no arbitrary endpoint/password field, and remains responsive while inventory loads.

- [ ] **Step 9: Commit the application shell**

```bash
git add src/main.rs src/app src/session src/connection.rs src/vnc/messages.rs src/vnc/client.rs tests/app_state_contract.rs tests/display_resize_contract.rs
git rm src/app.rs
git commit -m "feat: add RustedOutClient session workspace"
```

### Task 12: Implement the explicit loopback-only TigerVNC fallback

**Files:**
- Create: `src/fallback/mod.rs`
- Create: `src/fallback/password_file.rs`
- Create: `src/fallback/relay.rs`
- Create: `tests/fallback_contract.rs`
- Modify: `src/session/manager.rs`
- Modify: `src/app/actions.rs`
- Modify: `src/app/view.rs`
- Modify: `Cargo.toml`
- Modify: `Cargo.lock`

**Interfaces:**
- Consumes: verified running VM, verified SSH master, fresh `ProxyTicket`, absolute executable fallback viewer path, fullscreen/view-only preferences.
- Produces: `TigerVncFallback::open` and owned `FallbackSession` cleanup.

- [ ] **Step 1: Write failing password-file tests**

Use a fixed synthetic ticket only in unit tests:

```rust
#[test]
fn password_file_is_obfuscated_private_and_removed() {
    let runtime = RuntimeDir::create().unwrap();
    let path;
    {
        let file = VncPasswordFile::create(&runtime, SecretString::from("Ab12Cd34".to_owned())).unwrap();
        path = file.path().to_owned();
        assert_eq!(mode(&path), 0o600);
        assert_eq!(std::fs::read(&path).unwrap().len(), 8);
        assert!(!std::fs::read(&path).unwrap().windows(8).any(|w| w == b"Ab12Cd34"));
    }
    assert!(!path.exists());
}
```

Assert cleanup also occurs after viewer-spawn failure, accept timeout, relay failure, and cancellation.

- [ ] **Step 2: Write failing bind/viewer validation tests**

Assert the relay API has no bind-address parameter and `local_addr().ip()` equals `127.0.0.1`. Reject relative, missing, directory, and non-executable viewer paths. Assert exact viewer arguments include Shared, RemoteResize, VncAuth, PasswordFile, and `127.0.0.1::<port>`; include fullscreen/view-only only when requested.

- [ ] **Step 3: Write failing single-client relay tests**

Connect one loopback client and verify bidirectional bytes to a fake proxy stream. Attempt a second connection and assert it is refused because the listener closes after the first accept. After relay EOF, assert listener, proxy child, viewer child, and password file are gone.

- [ ] **Step 4: Run RED**

Run:

```bash
cargo test --test fallback_contract
```

Expected: compile failure because fallback modules do not exist.

- [ ] **Step 5: Implement TigerVNC password encoding and private temp ownership**

Port only the known TigerVNC password-file DES transformation from the existing helper. Use fixed key bytes `[0xE8, 0x4A, 0xD6, 0x60, 0xC4, 0x72, 0x1A, 0xE0]`, mode 0600, and the existing mode-0700 runtime directory. Never write the cleartext ticket.

- [ ] **Step 6: Implement one-client loopback relay**

Bind `TcpListener::bind((Ipv4Addr::LOCALHOST, 0))` internally, launch TigerVNC, accept one client with a 20-second timeout, close the listener, and relay with `tokio::io::copy_bidirectional`. Attach every resource to `FallbackSession` and use the same three-second owned-child cleanup contract.

- [ ] **Step 7: Surface fallback as explicit operator intent**

Connect `ViewerMode::TigerVnc`, the toolbar button, and Session menu to the fallback manager. Native failures show Open in TigerVNC but never invoke it automatically.

- [ ] **Step 8: Run GREEN and commit**

Run:

```bash
cargo fmt --all
cargo test --test fallback_contract
cargo test --all-targets
cargo clippy --all-targets -- -D warnings
```

Expected: PASS and `lsof -nP -iTCP -sTCP:LISTEN` shows no listener left by completed tests.

```bash
git add Cargo.toml Cargo.lock src/fallback src/session/manager.rs src/app tests/fallback_contract.rs
git commit -m "feat: retain loopback TigerVNC fallback"
```

### Task 13: Add redacted diagnostics and end-to-end synthetic acceptance tests

**Files:**
- Create: `src/diagnostics.rs`
- Create: `tests/support/rfb_peer.rs`
- Create: `tests/end_to_end_native.rs`
- Create: `tests/diagnostics_contract.rs`
- Create: `tests/cli_end_to_end.rs`
- Modify: `src/cli.rs`
- Modify: `src/main.rs`
- Modify: `src/session/events.rs`
- Modify: `src/session/manager.rs`
- Modify: `src/app/view.rs`
- Modify: `Cargo.toml`
- Modify: `Cargo.lock`

**Interfaces:**
- Consumes: typed transport/session events and synthetic OpenSSH/RFB fixtures.
- Produces: `DiagnosticRecord`, `PublicError`, functional `list`, `open`, and `probe` commands, and complete synthetic first-frame/input/cleanup evidence.

- [ ] **Step 1: Write failing diagnostics-redaction tests**

Construct an internal error with dummy target, ticket, raw stderr, clipboard, and framebuffer bytes. Assert the exported JSON includes only app version, upstream base SHA, OS/architecture, profile display name, node, VMID, phases, durations, child exit status, and error category.

```rust
for forbidden in [
    "root@example.invalid",
    "Ab12Cd34",
    "PRIVATE KEY",
    "clipboard sentinel",
    "raw ssh sentinel",
] {
    assert!(!exported.contains(forbidden));
}
```

- [ ] **Step 2: Build a deterministic synthetic RFB peer**

The peer must:

1. send RFB 3.8;
2. offer only security type 2;
3. validate the synthetic DES challenge response;
4. send ServerInit for a 64x64 framebuffer;
5. send one non-black rectangle for each supported encoding;
6. record pointer, key, CAD, release-all, and bounded clipboard messages;
7. optionally emit malformed/truncated cases selected by the test.

The fixture contains no external host or guest data.

- [ ] **Step 3: Write failing native end-to-end tests**

Drive the fake SSH executable and synthetic RFB peer through the real session manager. Assert cached inventory, live refresh, VM revalidation, ticket environment use, first non-black frame, exact CAD sequence, reconnect with a distinct ticket, two different concurrent VM sessions, and complete cleanup after ten connect/disconnect cycles.

- [ ] **Step 4: Write failing CLI process tests**

Using `assert_cmd`, verify:

```rust
Command::cargo_bin("rustedoutclient").unwrap().arg("list").assert().success();
Command::cargo_bin("rustedoutclient").unwrap().args(["probe", "107", "--json"]).assert().success();
Command::cargo_bin("rustedoutclient").unwrap().args(["open", "107", "--viewer", "tiger-vnc"]).assert().success();
```

Fixture output contains no secret. Assert `probe --json` reports `vmid`, `first_frame_ms`, `frame_width`, `frame_height`, `non_black_pixels`, and `result`, but omits target and ticket.

Add `assert_cmd = "2"` under dev-dependencies.

- [ ] **Step 5: Run RED**

Run:

```bash
cargo test --test diagnostics_contract --test end_to_end_native --test cli_end_to_end
```

Expected: failures because diagnostics export and complete CLI orchestration are not wired.

- [ ] **Step 6: Implement diagnostics and CLI orchestration**

Use typed fields only; never redact by searching an already-built untrusted blob. `list` waits for live inventory and prints VMID/status/name. `open` launches the app focused on the selected VM. `probe` opens through native transport, waits for first non-empty framebuffer or timeout, emits text or JSON, cleans the session, and exits 0 only on a successful frame.

- [ ] **Step 7: Run GREEN and repeat resource-leak tests**

Run:

```bash
cargo fmt --all
cargo test --test diagnostics_contract --test end_to_end_native --test cli_end_to_end
cargo test --all-targets
cargo clippy --all-targets -- -D warnings
```

Expected: PASS. Run the ten-cycle test five times; each run reports zero owned processes and zero remaining runtime artifacts.

- [ ] **Step 8: Commit synthetic acceptance coverage**

```bash
git add Cargo.toml Cargo.lock src tests
git commit -m "test: prove native Proxmox console lifecycle"
```

### Task 14: Enforce CI, dependency policy, threat model, and operator documentation

**Files:**
- Create: `deny.toml`
- Create: `.github/workflows/ci.yml`
- Create: `.github/workflows/fuzz-smoke.yml`
- Create: `SECURITY.md`
- Create: `docs/threat-model.md`
- Create: `docs/configuration.md`
- Create: `docs/upstream.md`
- Create: `docs/native-acceptance.md`
- Create: `docs/migration.md`
- Modify: `README.md`
- Modify: `NOTICE`

**Interfaces:**
- Consumes: complete product and tests from Tasks 1-13.
- Produces: merge-blocking macOS CI, scheduled fuzz smoke, reviewed dependency policy, security reporting instructions, and evidence-safe operator runbooks.

- [ ] **Step 1: Add dependency/source/license policy**

Configure `deny.toml` to:

- deny vulnerabilities and yanked crates;
- warn on unmaintained crates only when no maintained replacement exists, with any exception carrying an exact crate/version/reason and expiry date;
- deny unknown registries and all git dependencies;
- allow crates.io only;
- allow MIT, Apache-2.0, Apache-2.0 WITH LLVM-exception, BSD-2-Clause, BSD-3-Clause, ISC, Unicode-3.0, Zlib, and MPL-2.0 after `cargo deny list` confirms actual transitive licenses;
- deny duplicate major versions unless documented with exact dependency paths.

Run `cargo deny check` and resolve findings by removal, upgrade, or a narrowly documented exception; do not add a broad wildcard exception.

- [ ] **Step 2: Add merge-blocking macOS CI**

`ci.yml` runs on pull requests and pushes to main with `runs-on: macos-26`, starts by asserting `uname -m` equals `arm64`, and installs `cargo-audit` 0.22.2 and `cargo-deny` 0.19.0 with `--locked`. It executes:

```bash
cargo fmt --all -- --check
cargo test --all-targets --all-features
cargo clippy --all-targets --all-features -- -D warnings
cargo audit
cargo deny check
```

It also runs `rg` policy checks for forbidden source terms and uploads no configuration, runtime directory, framebuffer, or log artifact.

- [ ] **Step 3: Add scheduled and manual fuzz smoke workflow**

Install `cargo-fuzz` 0.13.2 with `--locked`, build all targets, and run each committed corpus for 30 seconds. A crash uploads only the minimized synthetic input; first inspect it for secret-bearing bytes, though the workflow itself has no access to live configuration or secrets.

- [ ] **Step 4: Write the threat model and security policy**

Document assets, system OpenSSH boundary, known-hosts trust, local attacker assumptions, malicious/malformed RFB server input, compromised Proxmox account impact, process-environment ticket exposure, clipboard risk, dynamic-resolution request/response bounds and resize-storm controls, fallback listener boundary, supply-chain controls, and residual risks. State clearly that VNC Auth is not encryption and is allowed only inside verified SSH.

`SECURITY.md` directs reports through GitHub private vulnerability reporting for `adamgell/RustedOutClient` and defines supported versions once releases exist.

- [ ] **Step 5: Write configuration, migration, upstream, and acceptance docs**

Use `example.invalid` targets and synthetic VM names only in configuration docs. `docs/upstream.md` records exact base SHA and requires reviewed cherry-picks. `docs/migration.md` states the old file is read-only and not deleted. `docs/native-acceptance.md` contains the exact gate sequence from the spec and a sanitized evidence table.

- [ ] **Step 6: Run all local policy gates**

Run:

```bash
cargo fmt --all -- --check
cargo test --all-targets --all-features
cargo clippy --all-targets --all-features -- -D warnings
cargo audit
cargo deny check
./scripts/fuzz-smoke.sh 10
```

Expected: every command passes. Review `git grep -nEi 'password|ticket|private key|ssh_target|clipboard' -- docs README.md SECURITY.md` and confirm each match is explanatory synthetic text, not real infrastructure or a secret value.

- [ ] **Step 7: Commit governance and documentation**

```bash
git add deny.toml .github SECURITY.md docs README.md NOTICE
git commit -m "docs: define RustedOutClient security and acceptance"
```

### Task 15: Perform exact-head review, native lab acceptance, and reversible rollout

**Files:**
- Modify: `docs/native-acceptance.md`
- Create: `docs/acceptance/2026-08-29-labz1-native-console.md`
- Do not modify: `/Users/Adam.Gell/.local/bin/pve-vnc`
- Do not modify: `/Users/Adam.Gell/.config/pve-vnc/config.json`
- Do not modify: `/Users/Adam.Gell/Desktop/Open PVE VNC.command`

**Interfaces:**
- Consumes: clean exact RustedOutClient branch head, local trusted SSH configuration, live configured Proxmox node, running `labz1-cm01` VM, and existing TigerVNC helper.
- Produces: independent native acceptance evidence, unchanged rollback path, reviewed branch ready for merge, and a local side-by-side RustedOutClient install.

- [ ] **Step 1: Verify exact local and remote heads before acceptance**

Run:

```bash
git status --short --branch
git push -u origin feature/proxmox-console-foundation
git fetch origin
git rev-parse HEAD
git rev-parse origin/feature/proxmox-console-foundation
git diff --check
```

Expected: worktree is clean, local and remote feature heads match exactly, and `git diff --check` prints nothing.

- [ ] **Step 2: Run the complete verification suite at that head**

Run:

```bash
cargo fmt --all -- --check
cargo test --all-targets --all-features
cargo clippy --all-targets --all-features -- -D warnings
cargo audit
cargo deny check
./scripts/fuzz-smoke.sh 30
cargo build --release --locked
shasum -a 256 target/release/rustedoutclient
```

Expected: all gates pass and the acceptance record captures the exact HEAD and release-binary SHA-256 without copying any credential or host address.

- [ ] **Step 3: Run an independent code review**

Invoke the available code-review skill against the exact feature-branch diff. Resolve every actionable correctness or security finding with a focused test and commit, rerun Step 2, and update exact-head values. Treat CI, review, dependency audit, fuzzing, and live native acceptance as separate gates.

- [ ] **Step 4: Establish the side-by-side local install**

Run:

```bash
install -m 0755 target/release/rustedoutclient /Users/Adam.Gell/.local/bin/rustedoutclient
/Users/Adam.Gell/.local/bin/rustedoutclient --version
```

Expected: the new binary reports RustedOutClient version and the old `/Users/Adam.Gell/.local/bin/pve-vnc` file hash and modification time remain unchanged.

- [ ] **Step 5: Migrate non-secret configuration explicitly**

Launch RustedOutClient, inspect the proposed import of `ssh_target`, `node`, and fallback viewer, and choose Save only after confirming no password/ticket field is displayed or written. Verify mode 0700 on the app directory and 0600 on `config.json`. Do not print the file because it contains private infrastructure addressing even though it contains no credential.

- [ ] **Step 6: Establish the existing TigerVNC baseline**

Run:

```bash
/Users/Adam.Gell/.local/bin/pve-vnc list
/Users/Adam.Gell/.local/bin/pve-vnc open labz1-cm01
```

Observe inventory, successful viewer connection, framebuffer, pointer, and normal keyboard behavior. Record timing from operator Open to visibly rendered guest using a stopwatch or screen-recording timestamps; do not retain the recording after timing values are transcribed.

- [ ] **Step 7: Perform native RustedOutClient acceptance**

Run:

```bash
/Users/Adam.Gell/.local/bin/rustedoutclient list
/Users/Adam.Gell/.local/bin/rustedoutclient probe labz1-cm01 --json
/Users/Adam.Gell/.local/bin/rustedoutclient open labz1-cm01
```

Visibly verify exact VM selection, Windows boot/lock/desktop framebuffer correctness, pointer accuracy, normal keyboard input, Ctrl+Alt+Delete transition, Release All Keys recovery, fullscreen, fit, 1:1, view-only, clipboard default-off, bounded explicit clipboard when enabled, proxy-kill reconnect, and no duplicate tab for the same VM.

With read-only evidence, record the target VM's existing virtual display device and guest video-driver family. Enable Dynamic Resolution and request at least 1,600x900, 1,920x1,080, and the current usable fullscreen viewport. For each request record Requested/Pending/Applied and the observed framebuffer dimensions. If QEMU rejects the request or the guest does not resize within two seconds, verify RustedOutClient reports Rejected, Unsupported, or Timed out, remains connected, and Fit to Window continues to work. Do not change VM hardware or install a driver in this acceptance step; stop and request separate approval if that is needed to achieve Applied.

- [ ] **Step 8: Measure warm-open and concurrency acceptance**

Run `probe labz1-cm01 --json` ten times after the SSH master is warm and record only `first_frame_ms`. Confirm median is no greater than 2,000 ms and at least 30 percent below the ten observed Python/TigerVNC baseline timings. Open a second distinct running VM and verify both sessions remain responsive.

- [ ] **Step 9: Prove repeated cleanup and fallback**

Open and close the native session ten times. After each close, use app diagnostics plus read-only process/listener inspection to prove no RustedOutClient-owned proxy child, listener, password file, or completed-session runtime artifact remains. Then choose Open in TigerVNC and verify its loopback/SSH session works and cleans up.

Do not kill unrelated processes during inspection.

- [ ] **Step 10: Re-prove rollback and record the acceptance boundary**

Close RustedOutClient and rerun:

```bash
/Users/Adam.Gell/.local/bin/pve-vnc open labz1-cm01
```

Expected: the old workflow still works. Record exact commit, binary SHA-256, timings, tested VMIDs/names, macOS architecture, TigerVNC version, and observed UI transitions in `docs/acceptance/2026-08-29-labz1-native-console.md`. Exclude credentials, addresses, fingerprints, guest pixels, clipboard content, and raw stderr.

- [ ] **Step 11: Commit acceptance evidence and push the exact head**

```bash
git add docs/native-acceptance.md docs/acceptance/2026-08-29-labz1-native-console.md
git commit -m "docs: record RustedOutClient native acceptance"
git push -u origin feature/proxmox-console-foundation
```

Rerun the exact-head commands from Step 1 and wait for every required CI check. Do not replace the Python helper or Desktop launcher in this plan.

## Completion Gate

This plan is complete only when all fifteen task commits exist, local and remote feature heads match, CI/security/fuzz gates pass at that exact head, native live acceptance is recorded, TigerVNC fallback is proven, and the original Python/TigerVNC workflow remains unchanged and functional. A successful build, unit test suite, synthetic RFB peer, or one live connection alone is not completion.
