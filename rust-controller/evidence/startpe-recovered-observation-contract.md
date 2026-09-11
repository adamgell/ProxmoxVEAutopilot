# Authority-preserving StartPe recovered-observation contract

Design checkpoint only. No scheduler widening, runtime implementation, or new
authority is claimed. This refines `startpe-missing-capture-quarantine.md`.

## Two missing evidence boundaries

1. The route hash currently lives only in `fixture_start_pe_responses` (migration
   0018), inside the transaction that the controller SIGKILL proof rolls back.
   The surviving generic dispatch stores operation/run/attempt, request/plan hashes,
   and numeric `original_generation`; this is not the fixture's UUID generation,
   owner, and checkpoint channel. Reconstructing a hash from replacement process
   configuration does not prove that it was the original dispatched route.
2. An accepted StartPe effect does not establish its outcome. The daemon's
   `FixtureStartPeObservationV1::publish` separately requires the accepted task,
   successful task state, and running/unlocked power. The current death-at-write
   test preserves the accepted effect but publishes no StartPe completion bundle.
   It therefore cannot support Satisfied, even with perfect route recovery.

## Proposed typed seam

Use a distinct closed `FixtureRecoveredStartPeObservationV1` (proposed name),
constructed only by a bounded read-only fixture client. It is not Deserialize,
cannot convert to `FixtureStartPeResponseV1` / dispatch permit / release proposal,
and is never accepted by `record_fixture_start_pe_receipt`.

Its validated content must bind exact durable dispatch identity and request,
fixture identity, original route provenance, accepted-effect digest, and original
daemon timestamps. Its result distinguishes at least:

- No accepted effect: still uncertain, not permission to resend.
- Accepted effect without qualifying outcome: still Unknown; observation only.
- Accepted effect with exact independent completion publication: eligible only
  for the separately defined reconciliation evaluator, not original capture.
- Conflicting/missing identity, route, publication, or timestamps: fail closed.

The daemon read must validate its durable accepted effect and any publication
against the exact request/ledger binding. A raw JSON accepted-effect response or
caller-supplied receipt must not itself manufacture this closed validated value.

## Durable route-intent prerequisite

Before future fixture StartPe submission, persist an immutable original route-intent
binding atomically with the committed dispatch (or its existing fixture arming
transaction). It needs exact operation/attempt/request hash plus fixture identity,
original fixture generation/owner and sealed canonical channel provenance hash.
The binding is observation-routing evidence, not new send authority; the existing
one-use permit and supervisor checkpoint remain necessary.

The recovered-observation loader must compare independently configured sealed route
against that durable pre-dispatch binding. A replacement generation is rejected
unless a separate historical-read delegation contract authorizes it. Existing
dispatches without route intent remain quarantined; no inference from a later
response, operator string, or current configuration may backfill original intent.

The test harness's surviving parent-owned worker input can select a known fixture
route for read-only experimentation, but it is not an implemented general durable
controller route-intent store. Those claims must remain separate.

## Distinct reconciliation persistence

Store accepted recovered observations in a separate append-only evidence stream
anchored to the original dispatch, with dedupe/conflict checks and preserved clocks.
Do not insert semantic/original receipt rows to satisfy existing receipt-required
SQL gates. Define a typed recovered-observation evaluator input and a locked,
observation-only state transition before adding StartPe to due discovery.

Tests must prove route intent survives controller death, missing legacy intent
refuses recovery, same evidence replay does not refresh time, wrong route/attempt
or changed bytes conflicts, accepted-only remains Unknown, exact completed outcome
can progress only by the new evaluator, and every failure/retry leaves one send.

## Inspection evidence

Inspected migrations 0005, 0017, and 0018; current typed response loader; scheduler
due discovery; controller reconciliation entry; and daemon StartPe publication
validation. No runtime tests were run for this design-only checkpoint. Whitespace
validation passed. No production mutation or synthetic receipt is introduced.
