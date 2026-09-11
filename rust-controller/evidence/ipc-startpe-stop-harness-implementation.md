# Connected StartPe journal harness implementation

Source inspected: `ea0f7866`. This note specifies the missing implementation;
it does not claim a connected StartPe/stop outcome test exists.

## Exact blocker

Adding `Setup::StartPe` to
`crates/operation-controller/tests/fixture_prefix_process/mod.rs` is insufficient.
Its `run` builds `FixtureProvisioningPort`, whose `provisioning_checkpoint` in
`crates/pve-port/src/fixture_support/provisioning_port.rs` handles configured
ConfigurePe/Resize contexts and otherwise requires a Clone request. Its
`shared_history_provenance` reads only `self.checkpoint`, the legacy Clone
context. The StartPe API in `provisioning_port/start_validation.rs` is explicitly
read-only and rejects configured dispatch contexts. There is no controller-ready
StartPe submission/readback/provenance context to select in a new Setup arm.

A genuine accepted StartPe journal does already exist in the test
`crates/pve-port/tests/fixture_post_dispatch.rs::full_start_pe_case`. It submits
Clone, DiskCapacity, ConfigurePe and StartPe through the daemon, obtains the
ConfigurePe predecessor receipt through `accepted_stage_effect`, obtains fresh
power-aware supervisor authorization, and persists the actual StartPe receipt
and full publication. Its process-death branch uses the owned children in
`tests/start_full_process/mod.rs`. Those are useful primitives, but their
synthetic request identities are not a PostgreSQL dispatch/callback history.

## Bounded implementation sequence

1. Extract the successful four-stage journal setup from `full_start_pe_case`
   into a test-support module beside `fixture_post_dispatch.rs`. Accept registered
   requests/identities as arguments rather than generating alternate operation,
   attempt or request IDs. Return original accepted request/receipt bytes and
   typed stage identities. Keep malformed/replay assertions in the original test.
   Preserve the existing daemon-owned authorization and publication calls; no
   file or SQL write may manufacture an accepted effect.
2. Add a private StartPe context to `FixtureProvisioningPort`, patterned after
   `provisioning_port/late_configure.rs`. Construction binds the operation's
   checkpoint channel, original ConfigurePe identity/request/receipt and exact
   StartPe request. Its checkpoint may enter only the StartPe barrier; only the
   supervisor supplies power authorization. Submission must use existing daemon
   stage IPC and retain exact receipt bytes. Readback validates the daemon's full
   publication against that request/receipt before exposing evaluator facts.
   Explicitly derive sealed shared-history provenance from this context's
   checkpoint binding, without falling back to caller JSON or Clone state.
3. Extend `fixture_prefix_process::Setup` with that predecessor context. Run the
   registered fixture StartPe through `OsDeployController` and its configured
   credential sink. Require atomic arming, sink acknowledgement and durable
   exposure before the ordinary committed dispatch/checkpoint path. Bind the
   daemon's request to the store's dispatch; compare receipt and attempt across
   both histories before accepting StartPe Satisfied.
4. Continue PeRegister and PeComplete through their existing authenticated
   controller/store methods using the delivered credential, registered package
   and boot-files evidence. Use the success path without unrelated lease-expiry
   scenarios. Let the original 30-second grace elapse through database time and
   existing expiry adjudication. Claim/start PeEnsureStopped normally.
5. Build the independent versioned power sample from the same accepted StartPe
   journal and the current store-issued stop authority. Obtain admission through
   `OsDeployController::admit_fixture_stop`, select using the sealed shared
   history, then call `consume_fixture_stop_outbox_envelope`. Construct the
   proposal from these original typed inputs and compare every envelope digest.
   The helper returns bookkeeping inputs only; it must never release or submit
   a physical stop.

## Lifetime and recovery requirements

The daemon's maximum lifetime remains 60 seconds. The existing owned child uses
30 seconds. Do not stretch either bound to cover arbitrary callback/recovery
waits. Prepare the PostgreSQL fixture first, start the daemon for the bounded
physical prefix, and explicitly terminate/reap it after the accepted StartPe
publication if subsequent waits will exceed the remaining lifetime. Preserve
the owned directory, journal and full publication bytes. Restart using existing
restoration rules and read original accepted effects. Restart invalidates old
barrier authorization; acquire the new stop supervisor generation and a fresh
independent power sample, while retaining the original StartPe identity and
database deadline. Do not rewrite durable accepted history to the new generation.

The harness must re-establish the operation-port/supervisor provenance after
restart, and prove that an old generation cannot consume a new selection. If
the current provenance API cannot express original accepted history plus a new
supervisor generation, fix that join before adding the positive outcome test.

## Focused acceptance tests

- Existing full-publication and process-death tests still pass after extraction;
  attempts/effects stay four and byte-preserving restart assertions remain.
- The new controller StartPe path persists the identical request and receipt as
  the daemon. Wrong predecessor receipt/channel/operation/attempt fails before
  dispatch; missing full publication cannot produce Satisfied.
- The connected helper creates outbox selection and consumption exclusively
  through scheduler APIs. A second consume yields no second envelope.
- Outcome test: record Ambiguous without a receipt, replay identically after
  store reopen, refuse Accepted or a different sequence for the same selection.
  Separate fixtures test Accepted with a receipt and Refused without one.
  Count zero stop effects in the daemon throughout; these are outcome-recording
  proofs, not physical outcome claims.
- Restart between accepted StartPe and stop admission retains callback session,
  original deadline and logical attempts, while fencing old supervisor inputs.

Validation for this note: inspected the named source paths and existing
StartPe process recovery evidence. No runtime code changed or test result was
claimed. The positive helper is larger than an isolated Setup addition because
it requires the missing adapter send/readback/provenance context and its linked
database proof. Production and real Proxmox remain untouched.
