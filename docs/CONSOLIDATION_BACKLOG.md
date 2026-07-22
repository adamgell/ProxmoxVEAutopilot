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

- Backend (`autopilot-proxmox`, pytest): GREEN as of 2026-07-22. 1284 passed, 0
  failed, 1 xfailed, 17 integration skipped (correctly gated behind
  `--run-integration`). Was 10 failed before greening; see "Baseline greening"
  below.
- Frontend (`autopilot-proxmox/frontend`): `tsc --noEmit` clean, `vite build`
  clean.
- Agent (`autopilot-agent`, .NET 10): code compiles (1 nullable warning in
  `AgentUpdateService.cs`). The WiX MSI installer only builds on Windows; that
  step is expected to fail on macOS and runs on the Windows build host instead.

### Baseline greening (DONE 2026-07-22)

The 10 red tests were all test-side debt from recent refactors; zero production
source changed. Fixed via 5 parallel agents plus one isolation fix:

- `test_cockpit_ui.py` (4): cwd-relative template path bug (`Path("autopilot-proxmox/...")`
  doubled when pytest runs from `autopilot-proxmox/`); fixed the path idiom to
  match the passing sibling tests. Templates were NOT deleted (see follow-up).
- `test_sequence_builder_target_os.py` (2): stale route expectations
  (`/react/sequences` -> `/react/task-engine/sequences/...`) and a retired
  `/api/sequences/page` endpoint; re-pointed to live routes/endpoint.
- `test_ts_engine_startup.py` + `test_web.py` (2): stale `FakeConn` doubles
  missing the `execute()`/`commit()` surface the DB-init path now calls.
- `test_osd_v2_endpoints.py` (1): sequence gained `install_qga` /
  `install_qga_watchdog` steps; refreshed the expected list and added an explicit
  agent-before-heartbeat ordering assertion.
- `test_onboarding_pg.py` (1): the `onboarding_state` feature was un-landed;
  marked `xfail(strict=False)`. RESOLVED 2026-07-22: naughty-buck landed the real
  `onboarding_pg` + full test suite, and the xfail was removed.
- `test_sdn_endpoints.py::test_lab_create_uses_open_egress_and_snat_by_default`
  (found during full-suite verification): missing `lab_bubbles_pg.reset_for_tests`
  caused an "ACME Lab" unique-key collision by test order; added the reset.

FOLLOW-UP (test-debt cleanup, separate task): the 3 CloudOSD/task-engine Jinja
templates are dead-on-disk (all routes now redirect to the React SPA), yet ~10
content-assertion tests in `test_cockpit_ui.py` still validate them. A proper
cleanup deletes those template files together with ALL their assertion tests as
one consistent change.

## Parked branches (recommended land order)

Land one at a time, each as its own reviewed and test-verified merge. Re-check
"still applies" before each, since the large React work already on `main` may
have superseded parts.

1. **[SUPERSEDED 2026-07-22, branch retired]** `claude/wizardly-williamson-ab4e56`
   (+1) - Would have realigned OSDeploy endpoint tests to the React shell, but
   `main` already did this more thoroughly (its `/osdeploy/builder` 302-redirects
   to `/react/osdeploy?view=builder` with `/api/osdeploy/page` data + an SDN
   vnet-targets test). The branch's older `200`-with-`react-root` expectation would
   regress `main`. Nothing to land; branch deleted (local + origin).
2. **[LANDED 2026-07-22 as `5a0ad1e`]** `claude/naughty-buck-700ffc` (+32) -
   Onboarding / first-run setup wizard (persona/tenant/identity/review steps, live
   setup monitor, phase rail, setup-status projection, atomic launch). Backend +
   wizard pages + Settings-nav routing landed; backend 1326 pass / 0 xfail.
   FOLLOW-UP: re-add the landing-page onboarding "resume" hero to the current
   OutcomeNavigation shell index (the branch's hero targeted the old shell and was
   dropped during the merge).
3. **[LANDED 2026-07-22]** `codex/roadmap-four-tasks` (+11, merged clean as
   `f1f0d2f`) - fleet duplicate-row fix, lab-specific Gen 1 hash upload boundary,
   OSDeploy VirtIO/secure-boot/QGA hardening. Verified 1335 backend / 194 frontend
   / agent contract tests. The paired `wip/2026-07-22-osdeploy-smartdeploy` was
   found MOSTLY SUPERSEDED by this + greening; its unique self-contained
   scaffolding (SmartDeploy + Entra app-registration scripts + tests, LABZ1
   runbook) was extracted and landed as `c0d7009`. Its plaintext
   `vault_entra-ivy24.yml` was deliberately NOT landed (secrets; now gitignored).
   The wip branch is kept as a local snapshot but has no further unique value.
4. **[LANDED 2026-07-22 as `6e64823`]** `claude/mystifying-keller-8c1a01` (+12) -
   Was 2/3 superseded (files-delete + oem-profiles CRUD already independently on
   main). Landed the unique install-tracking soft-delete (`install_tracking_pg` +
   `/api/install-tracking` endpoints + test); dropped the branch's redundant
   `test_file_shelf_delete.py`; regenerated the OpenAPI client. Backend 1365 pass.
   Branch retired (local + origin).
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
