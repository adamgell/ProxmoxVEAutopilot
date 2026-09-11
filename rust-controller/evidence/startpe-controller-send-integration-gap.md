# StartPe controller send-path integration gap

Source inspection checkpoint, 2026-09-11. No code changes or runtime test claims.
This adds a concrete prerequisite to `startpe-database-death-during-write.md`.

## Why an owned StartPe worker alone is insufficient

`operation-controller/src/osdeploy/send.rs::submit_and_capture_once` accepts a
generic `P: ProvisioningFakePort`, consumes the dispatch permit, and receives a
semantic `MutationReceipt`. Every persistence retry then calls
`scheduler.record_osdeploy_pve_receipt(&capture, &receipt)`.

It does not call `record_fixture_start_pe_receipt`. The closed original IPC
response is currently exposed only by the concrete fixture adapter's
`captured_start_pe_response()` method. Neither the generic provisioning interface
used by this function nor the `ControllerFixturePort` interface exposes that
original response. The latter exposes sealed route provenance and checkpointing,
which are not substitutes for original response capture.

Consequently, extending `fixture_prefix_process::Setup` with StartPe and invoking
the existing controller would not reach the original-response/provenance INSERT
barrier. Its generic receipt could commit without the fixture response row. A test
that then kills that worker must not claim atomic route-persistence crash coverage.

The existing successful capture tests call the fixture writer explicitly from
`capture_start_pe_after_genuine_prefix`, outside this normal controller send path.
Those tests remain valid composition evidence, not controller integration proof.

## Next bounded implementation seam

1. Provide a feature-gated, closed original-response accessor at the controller's
   actual fixture-port boundary, with fail-closed absence for unbound/restored
   adapters. Keep production observers unable to implement the sealed fixture
   interface. Do not deserialize or recapture original responses from SQL.
2. Integrate that response into the existing send function after successful genuine
   IPC submission. Validate it with `bind_fixture_start_pe_response` and choose the
   atomic fixture writer for this fixture StartPe path. Do not silently fall back
   to semantic-only persistence when that path requires original route evidence.
3. Preserve the existing outer deadline, bounded storage retries, one consuming
   send, and uncertainty classification. Reuse one captured response across all
   write retries; no retry may cause a second IPC submission.
4. Only then extend the owned worker setup with ConfigurePe predecessor observation
   and StartPe route configuration. The child must acquire its own live dispatch
   permit and original capture. The parent owns supervisor authorization and the
   DB barrier, observes the blocked INSERT, and uses the existing kill/reap protocol.
5. After child death, independently verify transaction rollback and durable fixture
   accepted-effect preservation. Recovery must not invent the lost capture or grant
   a replacement-generation route authority that the current API intentionally lacks.

The previous database-backend-death proof does not close this controller integration
gap. No production mutation, historical backfill, or synthetic authority is proposed.
