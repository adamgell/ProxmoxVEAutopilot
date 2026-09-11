# Immutable fixture delivery policy

The trusted fixture registration now persists `credential_sink_id` atomically
with the origin and workflow. NULL explicitly preserves historical physical-only
fixtures; a non-nil UUID requires credential delivery to that stable sink. Existing
origins cannot be updated, deleted, or silently upgraded. The new create API
rejects a nil sink and exact request replay includes the sink identity.

Every current scheduler StartPe path uses `locked_execution_with_cap` after
authority/run/operation/attempt/lease locks. That shared boundary now refuses a
credential-required origin with `CapabilityUnavailable`, including physical
dispatch, claim, resume, transition, and credential issuance paths. The policy is
loaded from the database rather than the local scheduler option. Unregistered
and physical-only fixtures retain their existing behavior; default builds do not
apply the fixture migration or query.

This implements admission isolation only. Credential-required runs deliberately
cannot progress through StartPe until the cohesive arm/delivery/exposure path is
implemented. The future credential path must explicitly separate policy-aware
locking from physical admission; removing the shared guard without adding that
check would reopen the alternate scheduler path. There is no callback authority,
production import, bearer delivery, or physical send introduced here.

Validation on macOS:

- `fixture_delivery_policy_is_immutable_and_closes_second_scheduler`: 1 passed,
  0 failed (1.41 seconds). Concurrent create replays select one origin; mode/sink
  substitutions fail; two independently constructed schedulers both reject
  StartPe before any session exists; the journal snapshot remains unchanged;
  policy mutation fails; migration replay and store reopen retain the sink.
- Default-feature `cargo check --offline --locked -p postgres-store`: passed.
- Existing origin rollback/race/reopen test with physical-to-delivery replay
  refusal: 1 passed (2.41 seconds).
- Feature-enabled all-target Clippy with warnings denied, formatting, and
  `git diff --check`: passed.

The two-scheduler proof is an admission race before predecessor completion. It
does not claim a physical-send race or credential delivery recovery proof.
