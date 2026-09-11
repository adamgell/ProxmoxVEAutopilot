# Bound fixture StartPe dispatch and explicit readback

Baseline: `cca20d5e`. `FixtureProvisioningPort` now accepts a private
`LateStartContext` via `with_late_start_after_configure`. Construction binds the
operation, supervisor generation/owner and original ConfigurePe identity/request,
and excludes other dispatch contexts. The context exposes sealed provenance from
that bound checkpoint client, following the ConfigurePe context pattern.

At checkpoint, the exact StartPe request must match the original predecessor
operation/attempt/plan and VM expectations. The context enters and waits for the
existing supervisor barrier. It does not install power, authorize release or
invent a receipt. Submission consumes the local one-use state before IPC and
uses the daemon's existing predecessor-aware mutation client. A transport failure
therefore leaves the adapter consumed, with outcome unknown; it cannot resend.

`validate_bound_start_pe` uses the captured daemon receipt and exact bound
identity/request to read the full publication. Missing receipt/publication is
`None`, never a success claim. Generic evaluator inventory/provisioning reads are
explicitly unavailable for this context; mapping them into controller facts is
the next connected implementation step. No Setup/controller Satisfied claim is
made for this adapter slice.

The existing `full_start_pe_worker_death_preserves_exact_publication_and_ledger`
subprocess test now submits StartPe through the new adapter. It retains the
genuine Clone/Resize/ConfigurePe journal and supervisor power authorization. It
checks wrong-stage checkpoint refusal, pre-checkpoint send refusal, absent
readback, one actual task receipt, duplicate-send refusal, and exact full
publication readback. Existing daemon/reader process death, restart, byte identity
and four-effect count assertions still execute. The test obtains comparison
receipt bytes from `accepted_stage_effect`; it does not fabricate them.

Validation: focused subprocess test passed (1 test, 1.19 seconds); full
`fixture_post_dispatch` regression suite passed 12 tests (including two inert
child entrypoints). All-target/all-feature pve-port Clippy passed with warnings
denied. Formatting and whitespace checks pass.

Remaining gates: generic evaluator mapping, durable receipt restoration into
this context, Setup and PostgreSQL callback/grace integration, authentic stop
selection/consumption/outcome matrix and current-source Linux qualification.
The existing bounded daemon lifetime/restart rules are unchanged. No physical
stop, real Proxmox mutation or production deployment occurred.
