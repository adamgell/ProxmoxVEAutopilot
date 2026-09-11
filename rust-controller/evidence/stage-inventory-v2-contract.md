# Additive stage inventory v2

`FixtureStageInventoryV2` is the cluster-inventory portion of future complete
postconditions, not a replacement for the entire post-dispatch bundle.
Legacy `FixtureCloneReads` and its SHA-256-only identity schema are unchanged.

Each member includes the full typed configuration, power, complete coverage,
and a tagged identity. `pve_digest` preserves the opaque server revision exactly.
`canonical_config_sha256_v1` hashes the bytes produced by
`serde_json::to_vec(ProvisioningVmConfigV1)`, including the opaque revision and
original observation timestamp. It is not a hash of arbitrary input JSON, nor
the template fingerprint. Future serialization changes require a new algorithm
tag. The implementation computes and checks that hash rather than accepting a
syntactically valid string as identity proof.

Validated decoding requires externally trusted stage identity, typed request,
original receipt bytes, and durable acceptance/publication clocks. It checks
the receipt contract and byte hash, full operation/attempt/generation/owner/
request binding, fixture identity, post-acceptance time, matching configuration
timestamps, source and target membership, complete coverage, fake provenance,
unlocked/supported configurations, and unique VM, UUID, MAC and disk identities.
Unknown fields, oversized envelopes, partial/missing members, digest relabeling,
incorrect hashes and receipt re-encoding are refused. Serialization is via the
derived serde encoder; raw deserialization alone does not validate the contract.

This contract grants no dispatch or publication authority. Caller-provided
trusted inputs must eventually come from the durable ledger; decoding by itself
does not authenticate a collector or prove that the cluster scan was complete.
The observation may report stopped or running; validation does not decide
whether a requested stage has been satisfied.

Remaining gates: bind this inventory to full configuration/media/infrastructure
and durable task/power observations in one publication protocol; implement
durable replay and adapter installation; then prove controller progression.
No real qmstart, production/default path change, or controller satisfaction is
included.

Validation: v2 encode/decode and refusal test passed (1); pve-port fixture-ipc
library tests passed (36); full fixture_post_dispatch tests passed (9). Strict
all-target fixture-ipc Clippy for pve-port and operation-controller, workspace
fmt checking, and git diff whitespace checking passed. No PostgreSQL or real
infrastructure execution is claimed by these checks.
