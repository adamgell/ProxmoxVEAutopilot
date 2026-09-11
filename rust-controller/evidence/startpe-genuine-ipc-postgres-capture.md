# Genuine fixture IPC and PostgreSQL StartPe capture proof

The existing
`fresh_controller_clone_then_disk_capacity_then_configure_pe_reaches_satisfied`
test now continues its real fixture prefix through StartPe submission and
atomic original-response persistence. Clone, Resize and ConfigurePe still run
through the original controller/subprocess harness and must reach Satisfied.
No SQL stage-success rows or response capabilities are manufactured.

After ConfigurePe, its observation-only restored adapter supplies actual
validated IPC facts for StartPe preflight. The shared test collector now accepts
an explicit `ProvisioningFakePort`; existing callers still delegate to their
unchanged NativeFake fixture. This new path supplies the restored IPC adapter
and asserts that NativeFake recorded no provisioning submissions.

The scheduler claims and starts the real StartPe attempt, records collected
preflight evidence, reconstructs its request and commits a genuine dispatch
permit/capture. The StartPe adapter binds the original ConfigurePe identity and
request. The supervisor validates the original accepted predecessor receipt
and stopped/unlocked power read before authorizing the checkpoint. The consumed
store permit then submits once through the IPC adapter, producing the closed
original-response value used by `record_fixture_start_pe_receipt`.

The connected proof covers:

- An injected AFTER INSERT SQL failure at the fixture response row rolls back
  the original-response transaction. The strict store reload finds no semantic
  receipt, and the fixture response table has no row for the operation.
- Two schedulers concurrently retry the same original closed input; both return
  success through the idempotent transaction, retaining the accepted response.
- A further replay preserves the original receipt timestamp and operation
  revision. SQL response bytes equal the original IPC envelope byte for byte.
- A separately launched process opens PostgreSQL, uses the strict store loader,
  and reads the original envelope. Its PID differs from the test process and
  its revision, capture timestamp and response bytes match exactly.
- The IPC journal bytes remain unchanged through failed storage, retries and
  reload. Another submission through the consumed adapter is refused.

This is independent-process database readback after original-response capture,
not a kill-during-write/crash-commit proof. It does not yet provide a typed
fixture-response loader, configured fixture-route ownership join, conflicting
original-capture substitution matrix, complete StartPe outcome satisfaction,
callback/grace integration or physical stop. Those remain explicit gates.

The ignored SQL reload child is added to the owned Linux launcher's supervised
child exclusion list so a blanket `--include-ignored` run cannot invoke it
without its parent-controlled inputs. This changes test selection only; no
Linux image pin or current-source qualification claim changes.

Validation: focused connected test passed (1/1, 13.11 seconds) with three real
prefix worker subprocesses and one independent SQL reload child. All-feature
all-target operation-controller Clippy passed with warnings denied; the owned
Linux launcher tests passed (14/14). Formatting and targeted whitespace checks
passed. No production or real Proxmox mutation occurred.
