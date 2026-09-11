# Typed full StartPe restoration

`FixtureStartPeRestoration::observe_full` and
`FixtureReadClient::start_pe_full` now read the full persisted publication as
`FixtureStartPeFullPublicationV1`. The two socket reads share one deadline:
first restore the bounded compact durable record, then read the full bundle
with a 131072-byte frame cap. Missing compact evidence or a null full bundle
remains `None`; zero/oversized frames and invalid evidence are errors.

Daemon replay and client restoration use the same validated decoding path.
The client checks the checksum, original publication clock, exact durable
record, stage/request/original receipt binding, v2 identity semantics, complete
inventory, target/source configurations and power, media, and infrastructure.
The checksum is corruption detection, not authentication; the private fixture
socket/directory remains the trust boundary. No observation times are refreshed.

The real fresh-prefix IPC proof now checks typed absence, successful full
restoration, and equal restoration after daemon restart. Adversarial socket
responses substitute owner, generation, attempt, receipt hash, coverage, task,
storage, bridge and target power while recomputing the checksum. These still
fail deep validation. Zero/oversized response frames are refused before payload
allocation. Existing torn/malformed sidecar and duplicate publication proofs
remain green.

Validation: pve-port fixture-ipc library 36 passed; inventory-v2 1 passed;
publication/restart suite 9 passed. Strict all-target fixture-ipc Clippy for
pve-port and operation-controller, workspace fmt, and diff checks passed.

Remaining gates: general provisioning adapter integration, PostgreSQL controller
progression/worker-death proof, and later guest stages. This is a read-only
restoration API, not controller satisfaction, a dispatch capability, real
qmstart, or production readiness. No production/default path changed.
