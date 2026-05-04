# WinPE M1 e2e runbook (pve1)

## Goal
One full provisioning run on pve1 using the WinPE path, demonstrating:
- WinPE boots from the attached ISO,
- partition + WIM apply + driver inject + validate succeed,
- Specialize + OOBE + FirstLogon complete,
- QGA registers within the existing wait window,
- Hash capture (OOBE-pass FLC) emits the expected file.

## Prereqs
- Phase E completed: WinPE ISO uploaded to `isos:iso/`.
- Phase H2 completed: blank template VMID = 9001 exists.
- `vault_autopilot_winpe_token_secret` set in vault.yml.
- Web app restarted.

## Steps

1. From the web UI, open `/provision`.
2. Pick a sequence with `produces_autopilot_hash=true` and `hash_capture_phase=oobe`.
3. Set Boot mode = WinPE. Submit.
4. Open `/runs/<id>` (the redirect target).
5. Watch the `winpe` phase steps go pending -> running -> ok in order:
   `partition_disk`, `apply_wim`, `inject_drivers`, `validate_boot_drivers`,
   (`stage_autopilot_config` if Autopilot-enabled), `bake_boot_entry`,
   `stage_unattend`. Run state moves to `awaiting_specialize` once /winpe/done fires.
6. Watch the VM's Proxmox console: VM reboots; Setup Specialize banner
   appears; OOBE flashes through; first logon happens.
7. QGA reports in. Existing hash-capture FLC writes the hash file.
8. Run state advances to `done` once the playbook posts to `/api/runs/<id>/complete` after the post-Specialize reboot cycle (wired in Task H4).

## Failure-mode triage

- **Run state stuck at `queued`**: Ansible never POSTed identity. Check
  `journalctl -u autopilot-flask` and the playbook log for the URL of the
  identity POST.
- **Run state stuck at `awaiting_winpe`** for more than 10 min: WinPE
  agent did not phone home. Open the VM console to see Invoke-AutopilotWinPE
  output. Common causes: NetKVM not loaded (no NIC), Flask host unreachable
  from the VM SDN, build-time `config.json` has the wrong base URL.
- **Run state = `failed`**: read `last_error` on `/runs/<id>`. The failed
  step's `error` column has the dism/diskpart/bcdboot tail.
- **VM never boots Windows after detach**: check `qm config <vmid>` shows
  `boot: order=scsi0` only. If ide2 is still listed, `/winpe/done` did
  not run; agent crashed.

## What "merged" looks like

`/runs/<id>` shows all phase-0 steps `ok`, run state `awaiting_specialize`,
the VM completes Specialize (existing wait_reboot_cycle returns), and the
existing hash-file watcher reports the hash captured.

## M2: pre-OS hash capture validation

1. Edit a test sequence; set Hash capture phase = WinPE; save.
2. Provision with Boot mode = WinPE.
3. On `/runs/<id>` confirm the action list now starts with `capture_hash`.
4. Confirm the captured hash appears in the existing hashes UI.
5. Compare against an OOBE-pass capture for the same VM; the
   `hardware_hash` value must match.

If the capture step fails on the live cluster (likely with
Get-WindowsAutopilotInfo's WMI provider expectations), evaluate the
OA3Tool path or direct-SMBIOS path before re-running. Each gets its
own follow-up task.
