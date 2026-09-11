# Frozen Linux AMD64 build: e0bb5f10

The exact Git archive at `e0bb5f10f66ee1084a30a92a127b5cefe6551294` built
successfully on the local OrbStack Docker host on 2026-09-11. The build context
included only committed `rust-controller` and the fixed `_test_long_sleep.yml`
playbook. It excluded concurrent worktree edits, untracked files and production
configuration. The source build argument matches this same commit.

The existing `restart-watchdog-capture.py` supervised the build with its unchanged
1,800-second bound. The command exited 0 in **948.555 seconds**; the receipt
confirms the direct child was reaped, its process group was clean, and the leader
did not leave a surviving group. No restart, retry or cleanup was performed.

Verified image identity:

```text
sha256:76c55d9151bc43a71cd24a57d334257e872ecd6d9086b652c53099a3c8e50cb0
architecture: amd64
OS: linux
CONTROLLER_GIT_SHA: e0bb5f10f66ee1084a30a92a127b5cefe6551294
image bytes: 6066411765
tag: rust-controller-qualification:e0bb5f10
```

The build log records successful workspace/example release compilation,
controller-service release compilation, all-feature test binary compilation,
and every existing Dockerfile focused test layer: provisioning port,
controller decision/support, artifact-index, fixture IPC, OSDeploy adapter and
selected service boundaries. The retained log is the authoritative per-command
test output. This does not execute the full owned PostgreSQL Linux qualification
or establish current callback/service integration acceptance.

Preflight used the explicit local Docker socket and returned Linux/aarch64,
21,014,999,040 bytes total VM memory, and Docker 29.4.0. Host free disk was
435,823,596 KiB, above the 18 GiB build guard. These are point-in-time build
prerequisites; they are not a refresh of the owned runner's separate 12 GiB
available-memory admission. `./skill.sh status` confirmed docs availability;
the docs search returned older general plans, so the checked-in Dockerfile and
historical bounded build invocation supplied the applicable build procedure.

During the build, source commit `957ada1a` changed
`crates/api-compat/src/callback_contract.rs` and
`crates/api-compat/tests/callback_contract.rs`. At observed HEAD `95444d97`, those
two paths differ from the frozen archive. Consequently the image is sealed to
`e0bb5f10`, not the later HEAD. The launcher pins remain unchanged: claiming the
newer source through a pin-only change would be invalid. A stable intended
runtime revision must be rebuilt before qualifying the newer code.

The retained preflight, build and image-inspection files are original watchdog
outputs. No real Proxmox or production controller mutation occurred. Production
readiness, the full port and deployment approval remain open.
