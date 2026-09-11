# StartPe route reload and old-schema upgrade proof

This closes the two explicitly untested boundaries in
`startpe-route-retention-implementation.md` without changing runtime code.

## Independent-process configured-route reload

The existing owned SQL reload subprocess now receives checkpoint channel and
binding configuration, independently constructs sealed provenance, and invokes
`load_fixture_start_pe_response_for_route`. It has neither the original adapter
nor the original closed IPC capture. It returns the accepted route hash alongside
the original response bytes, receipt clock, revision, and distinct process ID.
It also independently rejects the same binding on a different configured channel.

Red: the parent expectation for the route hash failed against the previous
unbound worker (`Null` versus expected hash), 13.45s. Green: the updated worker
and the combined migration checks passed below.

## Additive migration and absent historical provenance

At the end of the genuine IPC/PostgreSQL proof, the test removes only the new
provenance column from its owned disposable database to reconstruct the pre-0018
table shape. The already committed original response and semantic receipt remain
genuine; no response rows or original authority are fabricated.

Running the public migration API twice adds the nullable column idempotently and
leaves that retained row NULL. The configured-route loader refuses it. Replaying
the original closed capture also refuses it rather than attaching the hash later.
Unbound observation still returns the exact original bytes, and semantic receipt
timestamp/revision remain unchanged.

This is a reconstructed-old-schema upgrade proof using genuine current-run data,
not a claim that an archived historical deployment was executed. The test removes
captured provenance only inside a disposable test database; production is untouched.

## Exact checks

`RUST_MIN_STACK=16777216 cargo test --offline --locked -p operation-controller --features fixture-ipc --test postgres_fixture_clone fresh_controller_clone_then_disk_capacity_then_configure_pe_reaches_satisfied -- --exact --nocapture --test-threads=1`

Passed 1/1 in 14.00s, including three genuine prefix worker subprocesses and the
independent route-aware SQL reload process. The existing rollback, replay, route
conflict, immutable journal, and duplicate-send checks remain in the same proof.

`cargo clippy --offline --locked -p operation-controller --features fixture-ipc --test postgres_fixture_clone -- -D warnings`

`cargo fmt --all --check`

No kill-during-write, replacement-generation delegation, current-source Linux
runtime qualification, or production readiness claim is added.
