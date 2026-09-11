# StartPe configured-route retention: additive schema contract

Status: inspected, not implemented. This refines the route gap in
`startpe-typed-observation-loader.md`; it does not close that gate.

Repository MCP docs were reachable on 2026-09-11; the search `StartPe provenance`
returned no matches. The following findings come from the local Rust source.

## Exact existing seams

- `pve-port/src/fixture_support/provisioning_port/late_start.rs` already derives
  sealed provenance in `LateStartContext::provenance()` from the configured
  checkpoint client and original operation, generation, and owner. The successful
  submit path creates `FixtureStartPeResponseV1`, but drops that provenance.
- `postgres-store/src/scheduler/osdeploy/receipt.rs::retain_fixture_response`
  atomically inserts and exactly replays the original response. Neither its insert
  nor comparison currently includes provenance.
- Migration `0017_fixture_start_pe_responses.sql` creates an immutable table with
  `CREATE TABLE IF NOT EXISTS`. Editing that CREATE statement alone would not
  upgrade existing databases. Its immutable triggers also make a historical-row
  backfill inappropriate, independently of the missing source evidence.
- `load_fixture_start_pe_response(operation)` validates stored observation identity
  but accepts no independently configured sealed route object.

## Minimum truthful implementation

1. Capture the existing sealed provenance (or its canonical hash) in the closed
   response type at genuine successful IPC submission. Compute any fallible route
   validation before the physical fixture submit so it cannot manufacture a new
   post-submit failure window. Never populate original capture during restoration.
2. Add a new feature-gated, idempotent migration, rather than rewrite migration
   0017. Add a nullable hash column with a lowercase 64-hex constraint. NULL must
   mean historical evidence absent; never default it from current configuration.
3. Require non-null original provenance in the new capture insertion and compare
   it during exact replay in the same existing semantic-receipt transaction.
   Replaying against a historical NULL row must conflict; do not attach evidence
   after the original transaction.
4. Preserve the unbound observation loader's limited meaning. Add a distinct
   route-validated observation API accepting `FixtureSharedHistoryProvenanceV1`,
   not an arbitrary string supplied by the caller. Check original operation,
   generation, and canonical hash against the retained value. Missing hash or any
   difference must fail closed. The returned type must remain observation-only.
5. Explicitly limit the first API to the same original route. A replacement
   generation has a different sealed hash and must be rejected until a separate
   historical-read delegation contract exists. Equality must not grant checkpoint,
   callback, cancellation, physical stop, or send authority.

## Focused proof required before claiming the gate closed

- Genuine IPC capture retains the configured route hash; restored adapters still
  produce no original capture.
- Exact route succeeds after independent-process SQL reload. Different operation,
  generation, owner, or channel fails independently, even when response bytes and
  fixture IDs match.
- The existing forced transaction failure leaves no response/hash row, and
  concurrent exact replay preserves the original timestamp, revision, and hash.
- Upgrade a disposable database created under the old schema; migration preserves
  its historical NULL provenance and the route API refuses that row. Do not invent
  an original response to populate a fixture row solely for this test.
- Default-feature compile and migration behavior remain unchanged.

No runtime code, SQL schema, production service, or physical VM was changed for
this inspection checkpoint. No new runtime tests were run; validation is source
inspection and a whitespace check of this contract only.
