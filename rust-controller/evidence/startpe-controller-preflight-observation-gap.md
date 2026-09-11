# StartPe controller preflight observation seam

The send/capture integration is implemented, but the normal controller cannot yet
reach that StartPe send branch using only the current bound StartPe adapter.

## Verified limitation

The controller retains one resolved operation port across observations, checkpoint,
and dispatch (`with_fixture_ports` contract). Before dispatch its collection uses
that port. A port configured with `with_late_start_after_configure` routes inventory
and provisioning reads to `start_inventory` / `start_provisioning`.

Those functions require `validate_bound_start_pe` to return an accepted StartPe
full publication. Before dispatch the context has neither a bound request nor its
receipt, so validation returns None and configuration reads return
`PveReadError::TransportUnavailable`. A real controller must not interpret this
absence as ready preflight evidence.

The existing genuine composition helper explicitly avoids this limitation by
collecting preflight via a separate restored ConfigurePe observer:
`capture_start_pe_after_genuine_prefix` calls `s.collect_with(&context, observer)`
before constructing/submitting through the StartPe port. That is valid composition
evidence, but not proof that the same-port normal controller path works.

The focused adapter test now asserts the exact unavailable pre-dispatch target
configuration read while the genuine ConfigurePe predecessor already exists.
It does not fabricate a response, relax admission, or mark unavailable facts Ready.

## Required next seam before owned-controller SIGKILL proof

Give the bound StartPe adapter a validated ConfigurePe predecessor observation path
for preflight. Bind it to exact predecessor identity/request/original receipt and
the intended shared fixture history. Before StartPe dispatch, reads may use only
the daemon-validated predecessor publication. Once dispatch begins, do not silently
fall back to predecessor observations when the StartPe outcome is unavailable.

This requires an explicit state transition and tests for wrong predecessor bytes,
fixture/operation identity, channel/generation changes, missing publication, and
no fallback after dispatch. The current StartPe constructor accepts predecessor
identity/request but no predecessor receipt/validated observation source; merely
adding Setup::Start to the worker does not supply this contract.

Then the owned child can run normal controller admission and acquire genuine
StartPe permit/capture itself. The parent can observe the original-response INSERT
barrier and kill/reap that exact child. Until then, a manually pre-admitted worker
would not establish the requested end-to-end controller proof.

## Verification

`cargo test --offline --locked -p pve-port --features fixture-ipc --test fixture_post_dispatch synchronous_configure_publication_requires_resize_and_invalidates_on_restart -- --exact --nocapture --test-threads=1`

Passed 1/1 in 1.15s, including the new unavailable-preflight characterization.
Focused Clippy with warnings denied, formatting, and diff checks passed.

No runtime implementation changed, no production mutation occurred, and no
controller-process SIGKILL or production-readiness claim is added.
