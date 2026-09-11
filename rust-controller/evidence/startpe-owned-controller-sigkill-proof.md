# Owned StartPe controller SIGKILL at original-response transaction

The owned prefix subprocess now supports StartPe with a genuine ConfigurePe
predecessor publication as its preflight source. The child runs the normal
`OsDeployController`, acquires its own dispatch permit, submits once through the
supervisor-owned checkpoint, and reaches the integrated atomic fixture writer.
No dispatch permit, original response capture, or Ready fact is serialized into
the child.

## Independently observed death boundary

The parent owns an advisory lock and an AFTER INSERT trigger on the disposable
database's original-response table. It observes the child's committed dispatch,
arms its exact generated stage identity, and authorizes StartPe with genuine
ConfigurePe predecessor receipt and observed stopped/unlocked target power.

After the parent sees the child's original-response INSERT waiting at that lock,
it records fixture ledger bytes and requests the established owned-child kill.
The launcher asserts SIGKILL (signal 9), waits/reaps the child, and returns failure.
The parent releases the lock and independently waits for that exact PostgreSQL
backend to disappear before inspecting durable state.

Assertions prove:

- The dispatch identity remains committed and state remains Running, not success.
- No semantic receipt and zero original-response/provenance rows committed.
- The fixture journal remains byte-identical across death/SQL rollback.
- Four prefix attempts/effects exist: Clone, Resize, ConfigurePe, and one StartPe.
- The native in-memory fake received no submissions.

## Scope

This closes normal-controller StartPe invocation and controller-process death
during the original-response write. It does not restore the lost original capture
or prove successful replacement-controller continuation. The accepted StartPe
effect survives while SQL lacks its original response, so reconciling that state
needs a separate authority-preserving recovery contract. No resend, fabricated
receipt, SQL backfill, or replacement-generation route authority is introduced.

This uses the existing supervised prefix worker entrypoint, not an untracked child
type. The test's fixture daemon lifetime is 15 seconds for this death scenario;
no production service or physical VM is mutated.

## Verification

`RUST_MIN_STACK=16777216 cargo test --offline --locked -p operation-controller --features fixture-ipc --test postgres_fixture_clone start_pe_controller_death_during_write_rolls_back_original_response -- --exact --nocapture --test-threads=1`

The first complete proof passed 1/1 in 16.91s. With explicit SIGKILL signal and
backend-disappearance checks it passed 1/1 in 16.60s. Final source, additionally
asserting Running state and exact attempt/effect counts, passed 1/1 in 16.46s.
Focused operation-controller fixture-test Clippy with warnings denied, formatting,
and whitespace checks passed. No broader suite or Linux qualification is implied.
