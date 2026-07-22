# Consolidation Backlog

Single source of truth for the project reset done on 2026-07-22. The goal was
one clean `main` plus an ordered backlog of the work that was scattered across
branches, worktrees, and stashes.

## Foundation

- `main` is now exactly `origin/main` at `284ab56` (2026-06-24). This is the one
  source of truth. All other work is parked on branches listed below.
- Pruned in the reset: 16 extra worktrees (only the main checkout remains) and
  10 stale stashes (May 2026 codex "safety" captures; SHAs saved to the reset
  scratchpad and retained by the reflog for recovery).
- Nothing was lost. Every worktree was clean, and every commit is preserved by a
  branch or `origin/` ref.

## Health snapshot (2026-07-22)

- Backend (`autopilot-proxmox`, pytest): 1300 passed, 10 failed, 17 integration
  skipped (correctly gated behind `--run-integration`). The baseline is red.
- Frontend (`autopilot-proxmox/frontend`): `tsc --noEmit` clean, `vite build`
  clean.
- Agent (`autopilot-agent`, .NET 10): code compiles (1 nullable warning in
  `AgentUpdateService.cs`). The WiX MSI installer only builds on Windows; that
  step is expected to fail on macOS and runs on the Windows build host instead.

### The 10 failing tests on `main` (first work item: green the baseline)

- `test_cockpit_ui.py`: archive-history controls, cache-warming surface,
  readiness-live-after-completion, ubuntu target-OS palette/phases
- `test_onboarding_pg.py::test_init_creates_table_and_is_idempotent`
- `test_osd_v2_endpoints.py::test_osdeploy_task_engine_sequence_installs_agent_before_heartbeat`
- `test_sequence_builder_target_os.py`: builder renders, list-page target-OS badge
- `test_ts_engine_startup.py::test_registered_startup_initializes_app_database_url`
- `test_web.py::test_web_writes_service_health_heartbeat_on_startup`

These cluster around half-landed features (cockpit/onboarding/target-OS/health
heartbeat), which suggests some implementation or tests landed without their
counterpart. Several parked branches below likely complete them.

## Parked branches (recommended land order)

Land one at a time, each as its own reviewed and test-verified merge. Re-check
"still applies" before each, since the large React work already on `main` may
have superseded parts.

1. `claude/wizardly-williamson-ab4e56` (`bb009b4`, +1) - Realign OSDeploy
   endpoint tests to the React shell and MSI gate. Land first; it targets the
   failing OSDeploy/React-shell/MSI test families and may green part of the
   baseline.
2. `claude/naughty-buck-700ffc` (`9def8a0`, +32) - Onboarding / first-run setup
   wizard (live monitor, phase rail, setup-status projection over
   install_tracking). Largest coherent feature; likely completes the failing
   `onboarding_pg` and `web` health-heartbeat tests.
3. `codex/roadmap-four-tasks` (`b221136`, +11) plus
   `wip/2026-07-22-osdeploy-smartdeploy` (`a4945f9`, +1) - OSDeploy QGA MSI
   staging, secure-boot preflight block, VirtIO root preflight, plus the
   uncommitted SmartDeploy/WDS work and the Gen 1 hash lab-boundary. These two
   overlap; reconcile and land together. Note: the WIP introduced 2 regressions
   when rebased onto current `main` (`test_react_shell` fleet run-scoped agent,
   `test_sdn_endpoints` lab open-egress/snat default); fix those during land.
4. `claude/mystifying-keller-8c1a01` (`72c8b04`, +12) - OEM profiles CRUD plus PG
   storage and merged loader; install-tracking soft-delete endpoints.
5. `codex/installer-resume-spec` (`a06dcdf`, +5) - Installer resume and support
   flow with redaction.
6. `origin/codex/provision-three-columns` (`df2b803`, +2) - OSDCloud v2
   domain-join fix and the provision-launch three-column layout.
7. `codex/react-workflow-ux-pass` (`5aaa923`, +2) - Subnet edit form
   autopopulate (CIDR, DHCP range, SNAT).
8. `origin/codex/lab-dhcp-console-cleanup` (`8646847`, +1) - Lab DC readiness and
   VM console controls.

## Recovery notes

- The uncommitted work that was sitting on `main` before the reset is fully
  preserved on `wip/2026-07-22-osdeploy-smartdeploy` (`a4945f9`), including its
  merge-conflict resolutions against current `main`.
- Dropped stash SHAs are recorded in the reset scratchpad and remain applyable
  from the reflog until git garbage collection.
