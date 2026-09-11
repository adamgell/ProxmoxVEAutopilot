# StartPe durable power prerequisite

Local fixture-only milestone; no production/default path or callback changes.

The ledger now has an additive, versioned power-observation record. Old attempt,
effect and receipt encodings remain unchanged. Missing power records mean unknown
power. Records bind the exact accepted effect, operation, digest, stage, attempt,
owner, dispatch generation, byte-identical receipt digest, VM, daemon generation
and acceptance/observation timestamps. A running record requires the exact prior
stopped record and immediately preceding ConfigurePe effect for the same VM.
Replay revalidates these constraints; duplicate, mismatched and stale records
fail without appending. Millisecond-equal acceptance/observation timestamps retain
the existing publication contract; timestamps before acceptance are rejected.

The real supervisor IPC ConfigurePe publication now requires an explicit unlocked
stopped observation and durably writes this baseline before acknowledging the
publication. Restart retains the baseline but does not revive publications,
acceptance clocks or checkpoint authorization. A crash between ledger persistence
and publication acknowledgment fails closed; no automatic republish is claimed.

Validation: focused ledger unit test covers stopped/running replay, exact receipt,
owner mismatch, stale observation, duplicate replay and byte-preserving refusal.
The real IPC three-stage publication test covers wrong operation/owner/generation/
attempt, stale power evidence, unchanged ledger on refusal, single baseline and
restart. Existing StartPe IPC refusal tests prove no authorization consumption,
new attempt or effect. These are local fixture proofs only.

Remaining gate: StartPe is still refused before authorization consumption. The
running record is exercised at the ledger layer with an explicitly supplied
synthetic accepted effect, not claimed as IPC/controller execution. Atomic StartPe
transition admission, qmstart receipt/task publication and adapter/controller
integration remain required. Callback/session semantics and all subsequent stages
remain separate work; this is not production readiness or cutover evidence.

Fresh validation passed:

- `cargo test -p pve-port --features fixture-ipc` (including doctests; child-only entrypoint remains ignored).
- `cargo test -p pve-port --test fixture_daemon durable_fixture_log::power_tests` (default-feature ledger compatibility).
- `cargo clippy -p pve-port --features fixture-ipc --all-targets -- -D warnings`.
- `cargo clippy -p operation-controller --features fixture-ipc --all-targets -- -D warnings`.
- `cargo fmt --all -- --check` and `git diff --check`.

The PostgreSQL process-death matrix was not rerun in this bounded slice.
