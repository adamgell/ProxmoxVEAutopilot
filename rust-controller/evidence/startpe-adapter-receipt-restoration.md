# Observation-only restoration of the fixture StartPe receipt

After `8f3d1608`, `FixtureProvisioningPort::with_late_start_receipt` restores
the original StartPe response bytes into a fresh predecessor-bound adapter.
The existing binding validates the StartPe operation, attempt, request digest,
VM expectations and original ConfigurePe relationship. The receipt decoder
checks its structural request binding before installing one consumed state.

Restoration never enters or releases a checkpoint and cannot submit a mutation.
It marks submission consumed while leaving release false. Repeated restoration,
checkpoint entry and send through that adapter all fail. No public scheduler
grant, new durable acceptance or replacement receipt is produced.

Structural receipt parsing is not proof of acceptance. Every
`validate_bound_start_pe` read still calls the daemon's original accepted-effect
and full-publication validation. The focused test demonstrates this explicitly:
adding whitespace to otherwise identical receipt JSON can pass structural
restoration, but the subsequent read rejects the substituted bytes.

The genuine four-stage subprocess proof restores bytes obtained from the
accepted fixture journal and checks exact full-publication equality. It also
checks malformed receipt and wrong-stage request refusal, one-use restoration,
checkpoint/send refusal, and unchanged journal bytes. Existing worker/reader
death and daemon-restart checks remain in the enclosing proof.

This closes a fixture adapter observation seam. It does not connect PostgreSQL
receipt retrieval to that adapter, map generic evaluator facts, authenticate
callbacks, implement Setup/grace integration or issue a physical stop. Those
remain separate implementation and acceptance gates. No Linux runtime or
production-readiness qualification follows from this local proof.

Validation: focused subprocess proof passed (1/1, 1.25 seconds); the full
`fixture_post_dispatch` suite passed (12/12, 2.90 seconds, including two inert
child entrypoints). All-target all-feature pve-port Clippy passed with warnings
denied, and formatting and whitespace checks passed.
