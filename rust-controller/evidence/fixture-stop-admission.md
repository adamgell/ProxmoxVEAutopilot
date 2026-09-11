# Durable fixture stop admission

The supervisor-only `AdmitStop` checkpoint command records one immutable stop
admission frame. It requires an entered barrier for the exact current owner and
generation, identical submitted and supervisor-committed request bytes, the
request's evidence fence, and the structurally bound StartPe predecessor.

The ledger additionally resolves that predecessor's accepted receipt and durable
task-success/running-power publication. A receipt without that publication is
refused. The power observation must belong to the current daemon generation,
remain the latest power and effect for the VM, occur after the supplied current
lease check, and be no more than five seconds old. Guarded-grace decision time
must follow its due time and precede the lease check; admission must precede both
lease expiry and the original operation deadline.

`Scheduler::fixture_stop_authority` now collects the grace operation, selected
decision event, committed request evidence fence and current lease clocks from
PostgreSQL. It takes only a lease grant, acquires the normal authority/run locks,
reloads the full guarded-grace history, and validates the current owner, token,
attempt, generation, cancellation state and deadline. An uncommitted stop and a
different worker are refused. The operation creates no journal event or lease.

The IPC `AdmitStop` layer still accepts a supervisor-supplied DTO; it does not
independently query PostgreSQL. There is no connected supervisor consumer calling
the scheduler method or publishing fresh power after its database clock yet.
Consequently this method is a database observation, not an unforgeable dispatch
capability, and does not close the supervisor integration gate.

Admission leaves the barrier entered. It creates no stop attempt, effect,
receipt, task success or stopped-power fact, and cannot release stop dispatch.
Exact replay keeps the original admission clock and writes no second frame.
Different owner/generation or altered authority for the same admitted operation
is refused. Reload validates the historical frame against preceding durable
history without treating old authority as current release permission. Replacement
worker adoption of an admitted stop remains a separate implementation step.

## Verification on macOS

The PostgreSQL `fixture_pecomplete_authenticated_report_and_atomic_grace` proof
passed with authority collection assertions (1 test, 145.31 seconds). It reaches
the stop through authenticated completion and the real elapsed grace clock, then
proves pre-dispatch refusal, current-owner collection with the exact committed
request fence/deadline, and refusal of another worker with the same enabled
capabilities. The first run exposed a test identity mistake: its purported other
worker used the same scheduler worker ID. The corrected negative case uses a
different worker ID. Default compilation and strict all-feature/all-target
postgres-store Clippy passed after the correction, as did formatting and whitespace
checks.

The focused ledger test passed. It exercises accepted-receipt-only refusal,
grace/lease/deadline ordering, missing freshness, wrong daemon generation,
unchanged bytes on rejected admission, replay without clock renewal, changed
owner/generation refusal, reload, torn frames and checksum-valid invalid guard
history. It also asserts that admission adds no mutation or power state.

The stage contract suite passed 6 tests (2.34 seconds). The serial stage consumer
suite passed 3 parent tests (3.02 seconds), with its child entry point invoked by
the independent worker-death proof. That proof now also refuses worker-originated
admission and supervisor admission with merely structural StartPe history before
and after daemon restart.

Strict all-feature/all-target pve-port Clippy, default pve-port compilation,
formatting and whitespace checks passed.

## Remaining connected work

The ledger preserves refreshable current-power observations separately from the
immutable StartPe completion publication. General post-grace execution still
needs a supervisor IPC consumer that obtains the database authority and publishes
fresh power after that lease check. Successful stop dispatch,
task-success and stopped-power publication, restoration/reconciliation and
independent recovery are still gated. This evidence does not qualify production
or legacy behavior, and current-source Linux qualification remains outstanding.
