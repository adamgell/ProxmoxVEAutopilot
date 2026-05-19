# Installer Resume and Support Bundle Design

## Status

Design approved for implementation planning. A hard self-review pass has been
completed and folded into this spec.

This spec covers the Proxmox VE console installer flow in
`autopilot-proxmox/scripts/install-proxmox-ve.sh` and the lower-level phase
entrypoint in `autopilot-proxmox/scripts/init-proxmox-ve.sh`.

## Goals

- Detect what already exists before running installer phases.
- Recommend the safest restart point when the install is partial or dirty.
- Make the default path "continue recommended repair" instead of asking the
  operator to guess the right phase.
- Use optimistic repair for safe, idempotent, non-destructive work.
- Stop and ask before destructive actions, ownership conflicts, or ambiguous
  resource choices.
- Add a menu path that helps users create a GitHub issue with the current
  installer step, failed check, exit code, and sanitized evidence.
- Make failures diagnosable by recording stable step IDs and check IDs, not
  only human-readable labels.

## Non-Goals

- Do not change the meaning of the existing Foundation, Bootstrap,
  Operational, Runtime Config, Status, or Reset Dev Lab phases.
- Do not post issues to GitHub automatically.
- Do not upload support bundles automatically.
- Do not include secrets, tokens, private keys, passwords, raw vault files, or
  authorization headers in any issue draft or support bundle.
- Do not make destructive reset behavior automatic.
- Do not silently start large downloads unless the operator selected a download
  path or passed the matching download flag.

## Current Installer Shape

`install-proxmox-ve.sh` is a thin console UI over `init-proxmox-ve.sh`.

Current top-level actions:

- `menu`
- `guided`
- `foundation`
- `bootstrap`
- `operational`
- `runtime-config`
- `status`
- `reset-dev-lab`

`init-proxmox-ve.sh` already records setup evidence in
`autopilot-proxmox/output/setup/foundation_state.json`. It also performs live
work against Proxmox VE, storage, the Ubuntu controller VM, media, build-host
state, and artifact promotion.

The new design adds a detection and recommendation layer before the existing
menu and phase actions.

## Default Console Flow

The interactive installer should start with detection.

```text
Proxmox VE Autopilot Installer
================================

Scanning current stack state...
  ✓ PVE host tools available
  ✓ API token valid
  ✓ Role/ACLs present
  ✓ Controller VM found: autopilot-controller-01 (vmid=181)
  ✓ Controller health OK: http://192.168.2.181:5000/healthz
  ! Windows ISO missing
  ✓ VirtIO ISO present
  ! Build host not created
  ! Setup artifacts not promoted

Classification: PARTIAL INSTALL
Confidence: high
Current step: Bootstrap media
Failing check: Windows ISO media is missing

Recommended path:
  Bootstrap media -> Operational repair

Continue with recommended repair? [Y/n/details/menu/issue]:
```

The default answer is `yes` when the recommendation is high-confidence and the
next action is safe, idempotent, and non-destructive.

## State Detection

Detection should combine prior state with live probes. The state file is useful
but must not be trusted blindly when live Proxmox or controller evidence
contradicts it.

Evidence sources:

- State file: `output/setup/foundation_state.json`
- Proxmox probes: `qm list`, `qm config`, `qm status`, `pvesm`, `pveum`,
  `pvesh`
- Controller probes: stored controller URL, `/healthz`, `/api/version`, setup
  readiness endpoint when available
- Storage probes: ISO storage, Windows ISO, VirtIO ISO, promoted setup
  artifacts
- Build-host probes: VM exists, QGA reachable when available, controller-known
  build-host heartbeat when available
- Installer execution state: last action, current phase, current step, failed
  check, last command, and exit code

Detection output should be a compact model:

```text
classification
confidence
recommended_action
recommended_phases
current_step_id
current_step
failed_check_id
failed_check
blocked_reasons
dirty_reasons
conflicts
safe_repairs
sanitized_planned_commands
```

Detection must be read-only. `--action detect` and `--action status` must not
repair tokens, regenerate files, rescan by writing state, start VMs, stop
containers, sync source, publish setup state, or promote artifacts. They may
read files and run read-only Proxmox/controller queries. If a future probe needs
mutation to answer a question, it is not a detection probe and must move into a
repair phase.

The persisted detection output must be safe to share by default. It may include
resource names, step IDs, check IDs, boolean readiness, exit codes, and redacted
paths. It must not include token values, raw vault data, private key material,
authorization headers, cookies, or secret file contents.

## Stable Step and Check IDs

Every detected failure needs both a human label and a stable machine-readable
ID. The issue helper and support bundle must include the IDs so maintainers can
search issues and map failures back to code.

Recommended ID shape:

```text
phase.step.check
```

Examples:

- `foundation.pve_api.token_invalid`
- `foundation.controller_vm.identity_conflict`
- `foundation.controller_health.unreachable`
- `bootstrap.media.windows_iso_missing`
- `bootstrap.media.virtio_iso_missing`
- `bootstrap.storage.iso_storage_ambiguous`
- `operational.artifacts.metadata_mismatch`
- `operational.controller.config_stale`
- `support.redaction.secret_pattern_detected`

Each failure record should carry:

```text
phase
step_id
step_label
check_id
check_label
severity
exit_code
blocked_reason
recommended_action
```

The console can stay friendly, but the support path must preserve the IDs.

## State Classifications

### Clean

No useful prior state exists and no matching controller VM is found.

Recommended action:

- Foundation

### Partial Install

Some phases are complete, but setup is not operational.

Common examples:

- Foundation complete, Bootstrap incomplete
- Bootstrap complete, Operational incomplete
- Controller exists, but media is missing
- Media exists, but artifacts are not promoted

Recommended action:

- Resume from the next incomplete phase.

### Ready

The controller is healthy, media is present, build-host/setup artifacts are in
the expected state, and setup readiness is operational.

Recommended action:

- Status only, or Operational repair if drift is found.

### Drifted

The stack is mostly usable, but one or more idempotent repair steps should run.

Examples:

- Runtime config is stale
- Token or ACL needs revalidation
- Setup state needs republishing
- Media scan needs refresh
- Artifact promotion needs to be retried with matching metadata

Recommended action:

- Operational repair or Runtime Config repair.

### Conflicted

Live evidence and recorded state disagree in a way that may affect resource
ownership or safety.

Examples:

- State file says controller VMID `181`, but that VMID belongs to another VM.
- A VM named `autopilot-controller-01` exists with a different VMID than the
  state file.
- A target storage or bridge cannot be selected confidently.
- Existing media or artifacts do not match expected metadata.

Recommended action:

- Stop and ask the operator to choose.

## Confidence Rules

### High Confidence

All required evidence agrees and the next action is safe.

Behavior:

- Default prompt answer is `yes`.
- Run recommended repair if the operator presses Enter.

### Medium Confidence

Most evidence agrees, but one or more values are stale or missing.

Behavior:

- Default to recommended repair only when every planned operation is
  idempotent and non-destructive.
- Show the stale or missing values in the detection summary.

### Low Confidence

Evidence is missing, contradictory, or insufficient to pick a safe restart
point.

Behavior:

- Do not run anything by default.
- Send the operator to details or manual phase selection.

## Optimistic Repair Policy

The approved policy is optimistic repair.

The installer may automatically run safe, idempotent, non-destructive repairs
from the recommended point. It must stop and ask before destructive work or
ambiguous ownership decisions.

### Allowed Without Extra Confirmation

- Revalidating the PVE API token, role, and ACLs.
- Rebuilding missing local state from live evidence only when ownership is
  proven by expected names plus at least one additional signal, such as matching
  VMID from config, controller health identity, known source path, expected
  generated artifact metadata, or an existing state value that does not
  conflict.
- Syncing controller runtime config.
- Re-scanning media and storage.
- Publishing setup state to the controller.
- Retrying controller health checks.
- Promoting artifacts when hashes and manifests match.
- Repairing setup-state publication after a previous partial run.

### Always Requires Confirmation

- Deleting or resetting VMs.
- Removing generated or downloaded media.
- Choosing between conflicting controller VM identities.
- Overwriting artifacts when metadata does not match.
- Proceeding when Proxmox target storage or bridge is ambiguous.
- Any `reset-dev-lab` path.

Large downloads are not automatic repair. If Windows or VirtIO media is missing,
the recommended path may guide the operator into a download choice, but it must
not start a Windows ISO download unless the operator selected automatic media
download in the current prompt or supplied `--download-windows` /
`--windows-iso-url`.

Artifact promotion is safe only when source metadata, expected artifact type,
hashes, and destination identity agree. A filename match alone is not enough.

## Resource Ownership Rules

The installer must avoid claiming unrelated customer resources just because a
name or VMID looks familiar.

High-confidence ownership requires:

- Expected resource name, and
- No conflict with the recorded state file, and
- At least one supporting signal:
  - expected VMID in state/config,
  - expected controller URL or health identity,
  - expected generated metadata file,
  - expected source path under `CONTROLLER_REMOTE_ROOT`,
  - expected artifact manifest/hash metadata,
  - expected seed/build-host metadata.

Name-only discovery is medium confidence at best. VMID-only discovery is medium
confidence at best. If name and VMID point to different resources, the state is
conflicted and must not auto-run.

## Main Menu

When the operator enters `menu`, show:

```text
Main menu
---------
  1) Continue recommended repair
     Runs the safest next phase based on detected evidence.

  2) Guided install / repair
     Foundation -> Bootstrap -> Operational.

  3) Detection details
     Shows state file values, live probes, dirty reasons, confidence,
     blocked reasons, and exact commands.

  4) Manual phase selection
     Advanced operator control.

  5) Configure install inputs
     Node, storage, controller IP, controller VMID, bridge, media source.

  6) Show one-liners
     Copy/paste commands for unattended or targeted repair.

  7) Create GitHub issue / support bundle
     Writes a sanitized issue draft that includes the current step,
     failed check, exit code, state classification, and evidence.

  0) Quit without changes
```

## Manual Phase Menu

```text
Choose phase
------------
  1) Auto-detect again
  2) Foundation
  3) Bootstrap media
  4) Operational repair/promote
  5) Runtime config repair
  6) Status only
  7) Create GitHub issue / support bundle
  8) Reset disposable dev lab
  9) Return to main menu
  0) Quit
```

## Per-Step Behavior

### Detect

Reads state and runs live checks.

Outputs:

- State classification
- Confidence
- Stable step ID
- Stable failed check ID
- Current step
- Failing check
- Dirty reasons
- Blocked reasons
- Conflicts
- Recommended action
- Planned commands

### Foundation

Safe repairs:

- Host prerequisites
- PVE token
- Role and ACLs
- Storage and bridge detection
- Secrets
- Controller VM discovery
- Controller source sync
- Controller runtime config
- Controller health

Pause conditions:

- Controller VM identity conflict
- VMID/name mismatch with unclear ownership
- Destructive recreate would be required

### Bootstrap

Safe repairs:

- PVE token revalidation
- Windows media scan
- Official Microsoft download flow when selected
- Direct Windows ISO URL flow when selected
- VirtIO media scan/download
- Setup-state publication
- Blank template detection
- Build-host discovery

Pause conditions:

- Windows media source is required and was not provided
- Storage target is ambiguous
- Existing media conflicts with expected identity or metadata

### Operational

Safe repairs:

- Controller health verification
- Runtime config sync
- Media rescan
- Artifact promotion when metadata matches
- Setup-state publication

Pause conditions:

- Artifact overwrite would be required and metadata does not match
- Controller identity does not match known state

### Runtime Config Repair

Safe repairs:

- Token/config/media state sync
- Setup-state republish
- Controller runtime config refresh

Pause conditions:

- Controller cannot be identified confidently

### Reset Dev Lab

Always prompts before running.

The prompt must make clear that this is for disposable labs and may remove
generated VMs and optionally generated/downloaded media.

## GitHub Issue and Support Bundle Helper

Menu option `7` creates a local GitHub issue draft and optional sanitized
support bundle.

The helper must not post to GitHub automatically. The operator reviews the
draft before copying it into a public issue.

If no failure is currently recorded, the helper should create a "setup support
snapshot" draft instead of inventing a failure. It must clearly say no failed
check was detected.

### Console Flow

```text
Create GitHub issue / support bundle
------------------------------------

This will create a sanitized local issue draft.
It will not upload secrets or post to GitHub automatically.

Detected failure:
  Step: Bootstrap media
  Step ID: bootstrap.media
  Failed check: Windows ISO media is missing
  Check ID: bootstrap.media.windows_iso_missing
  Last command: init-proxmox-ve.sh --phase bootstrap --resume --download-virtio
  Exit code: 20
  Blocked reason: media_ready=false, windows_iso_ready=false

Include optional local environment details?
  PVE version, node name, storage names, VMIDs, controller URL host/IP.

Include these details in the issue draft? [Y/n]

Writing:
  output/support/github-issue-2026-05-19-1829.md
  output/support/support-bundle-2026-05-19-1829.tar.gz

Next:
  1) Print issue draft
  2) Show GitHub new-issue URL
  3) Return to menu
```

The GitHub URL should open the repository's new-issue page without embedding a
large prefilled body by default. The local issue draft is the source of truth.
If a future implementation offers a prefilled issue URL, it must ask first,
warn that URL query strings can leak through browser history or logs, and keep
the body short enough for browser/server URL limits.

### Issue Draft Shape

````markdown
# Installer blocked during Bootstrap media

## Current step
- Installer action: recommended repair
- Phase: bootstrap
- Step: Bootstrap media
- Step ID: bootstrap.media
- Failed check: Windows ISO media is missing
- Check ID: bootstrap.media.windows_iso_missing
- Exit code: 20
- State classification: partial_install
- Confidence: high

## What I expected
The installer should find or download Windows media and continue to Operational repair.

## What happened
Bootstrap stopped because Windows ISO media was not present and no Windows download source was available.

## Recommended by installer
Run Bootstrap media after providing a Windows ISO source:

```bash
bash install-proxmox-ve.sh --action bootstrap --resume --download-windows --download-virtio
```

## Evidence summary
- PVE API token: valid
- Controller VM: found and healthy
- Controller URL: redacted-or-confirmed-local
- VirtIO ISO: present
- Windows ISO: missing
- Build host: not created
- Setup artifacts: not promoted

## Recent installer log
```text
[installer] running bootstrap
[pve-init] media_ready=false
[pve-init] windows_iso_ready=false
Media gate is still blocked.
```

## Redaction
The support helper removed tokens, passwords, private keys, cookies, and authorization headers.
````

### Always Include

- Current action, phase, and step.
- Stable step ID.
- Failed check ID, failed check label, and exit code.
- State classification and confidence.
- Recommended next command.
- Sanitized state summary.
- Recent installer log tail.
- Support bundle path, if generated.

### Optional With Operator Confirmation

- PVE version.
- Node name.
- Storage names.
- VMIDs.
- Controller URL host/IP.
- Selected bridge.
- ISO filenames or volume IDs.

### Never Include

- PVE API token values.
- API token IDs if they include secret material; token names are allowed.
- Bearer tokens or authorization headers.
- Passwords.
- Secret file contents.
- Private SSH keys.
- Public SSH keys only when the operator explicitly confirms.
- Cookies.
- Raw vault files.
- Raw `.env` files.
- Full controller `agent.json` or other agent config files.
- Unredacted URLs containing credentials or signed download tokens.

### Redaction Requirements

Redaction must be fail-closed. If the helper detects secret-looking content
after redaction, it should refuse to write the support bundle and should write
only a minimal issue draft that explains redaction failed.

At minimum, redaction must cover:

- `PVEAPIToken=...`
- `Authorization: Bearer ...`
- `password`, `passwd`, `secret`, `token`, `apikey`, `api_key`, `client_secret`
- PEM private key blocks
- SSH private key blocks
- Cookie headers
- `.env` key/value lines
- JSON fields whose names contain `token`, `secret`, `password`, `key`,
  `authorization`, or `cookie`

The support bundle should include a redaction report listing which file types
were included, which were skipped, and which redaction patterns matched. The
report must not include the matched secret value.

## Failure Footer

Any phase failure should end with a useful footer.

```text
Bootstrap media did not complete.

Step: Bootstrap media
Step ID: bootstrap.media
Failed check: Windows ISO media is missing
Check ID: bootstrap.media.windows_iso_missing
Exit code: 20

Next actions:
  1) Retry Bootstrap with official Microsoft download
  2) Retry Bootstrap with a direct Windows ISO URL
  3) Wait for manual media upload
  4) Create GitHub issue / support bundle
  0) Return to main menu
```

## CLI Surface

The existing actions remain.

New or adjusted actions:

- `--action detect`
- `--action recommended`
- `--action support`
- `--support-print`
- `--support-no-bundle`

Existing `--action menu` should run detection first, then show the recommended
prompt.

Existing `--action status` should show the same detection model in read-only
form.

Unattended mode:

- `--action recommended --yes` may run only if the recommendation is safe and
  high-confidence.
- If confidence is low or conflicts exist, unattended mode must exit non-zero
  with the detected reason and support helper path.

## Files and Boundaries

Expected implementation files:

- `autopilot-proxmox/scripts/install-proxmox-ve.sh`
- `autopilot-proxmox/scripts/init-proxmox-ve.sh`
- Optional helper under `autopilot-proxmox/scripts/` if the support bundle or
  detection logic becomes too large for the shell UI.
- Tests under `autopilot-proxmox/tests/` if shell behavior is already covered
  by local test patterns or can be covered with small script-level tests.

Suggested output paths:

- `autopilot-proxmox/output/setup/foundation_state.json`
- `autopilot-proxmox/output/setup/installer_detect.json`
- `autopilot-proxmox/output/setup/install.log`
- `autopilot-proxmox/output/setup/install-last-failure.json`
- `autopilot-proxmox/output/support/github-issue-<timestamp>.md`
- `autopilot-proxmox/output/support/support-bundle-<timestamp>.tar.gz`

`install.log` should contain the installer UI action log and phase invocation
summary. It should not capture raw shell tracing with secrets. If `set -x` is
ever enabled for debugging, the support helper must refuse to include that log
unless redaction succeeds.

`install-last-failure.json` should be written when a phase exits non-zero. It
should contain only sanitized fields:

```text
timestamp
action
phase
step_id
step_label
check_id
check_label
exit_code
classification
confidence
blocked_reasons
recommended_action
sanitized_planned_commands
```

## Acceptance Criteria

- First interactive launch runs detection before showing the menu.
- A partial install recommends the next safe phase.
- High-confidence safe recommendations default to continue.
- Conflicted states do not auto-run.
- Reset Dev Lab remains explicitly confirmed.
- Menu option `7` creates a sanitized GitHub issue draft.
- Issue drafts include current step, failed check, exit code, classification,
  confidence, stable step ID, failed check ID, recommended command, and recent
  sanitized logs.
- Issue drafts and support bundles do not contain tokens, passwords, private
  keys, raw vault files, cookies, or authorization headers.
- `--action status` shows the detection model without mutating the system.
- `--action recommended --yes` refuses to run if confidence is low or conflicts
  exist.
- `--action detect` and `--action status` are read-only and do not modify the
  state file, controller, Proxmox resources, media, artifacts, or VMs.
- Missing Windows media does not silently start a large download unless the
  operator selected a download path or passed the matching CLI flag.
- Name-only or VMID-only resource discovery is not enough for high-confidence
  ownership.
- The support helper refuses to write a full bundle if redaction fails.
- Manual phase selection uses option `7` for the GitHub issue/support helper;
  Reset Dev Lab moves to option `8`.

## Test Plan

Implementation should include script-level tests or small helper tests for the
new decision logic. The tests should not require a real Proxmox host.

Required cases:

- Clean state recommends Foundation.
- Foundation-complete state recommends Bootstrap.
- Bootstrap-complete state recommends Operational.
- Ready-but-drifted state recommends Operational repair or Runtime Config
  repair.
- Conflicting controller VMID/name state refuses auto-run.
- Name-only discovery is not high confidence.
- `--action detect` does not call mutation helpers.
- `--action status` does not call mutation helpers.
- `--action recommended --yes` runs only high-confidence safe recommendations.
- `--action recommended --yes` exits non-zero for conflicts.
- Missing Windows media prompts or blocks instead of silently downloading when
  no media download option was selected.
- Failure footer includes step label, step ID, check label, check ID, exit code,
  and issue-helper option.
- Issue draft includes step ID and check ID.
- Redaction removes token/password/key/header patterns.
- Support bundle generation fails closed when redaction leaves secret-looking
  content.

## Open Implementation Notes

- Keep the existing phase functions as the source of real mutations.
- Prefer shell functions for small detection steps, but split support bundle
  formatting/redaction into a helper if it becomes hard to test in Bash.
- Treat live evidence as newer than the state file when they disagree, unless
  live evidence creates an ownership conflict.
- All support output is local-first. The installer may print a GitHub new issue
  URL, but it should not post automatically.
