# Rust controller restart/recovery and compatibility evidence

Date: 2026-09-10  
Worktree: `codex-rust-controller-restart-20260909`  
Scope: local macOS only; production `192.168.2.4` and real Proxmox remain read-only.

## Restart and recovery

The following real-PostgreSQL native-controller tests passed individually with `--include-ignored --nocapture --test-threads=1`:

- `delayed_completion_retains_grant_but_restart_cannot_adopt`
- `crash_after_dispatch_before_submission_is_unknown_without_replay`
- `crash_after_receipt_reloads_original_acceptance_and_never_replays`
- `recovered_clone_continues_configure_and_start_with_aged_infrastructure`
- `configure_response_loss_requires_fresh_reconciliation_without_replay`

The combined command output is `full.log`; the initial restart case is also retained in `stdout.log`. These tests prove restart fencing, unknown-state recovery, receipt reload, aged-infrastructure continuation, and response-loss reconciliation without replay authority. They are controller/database recovery proofs, not independent-process service takeover or production cutover proof.

## Python compatibility boundary

The current Python-side contracts passed under explicit Homebrew Python 3.12:

- producer contract: 2/2 (`python-producer.log`)
- proof-wait contract: 5/5 (`python-proof-wait.log`)
- proof-coordination contract: 1/1 (`python-proof-coordination.log`)

The Rust `api-compat` crate also passed 18 runtime tests and 2 intended compile-fail documentation tests. These checks establish the sanitized, duplicate-aware Python-to-Rust contract; they do not claim that the legacy Python/Ansible writer is quiesced or that a live dual-writer handoff has been performed.

## Readiness impact

This evidence closes the local controller/database restart-recovery and compatibility-contract gates. Remaining production-candidate gates are independent-process service recovery, explicit Python/Ansible single-writer handoff/quiescence, rollback/export sealing, non-production sacrificial workflow approval, and operator acceptance. No deployment or mutation is authorized by this record.
