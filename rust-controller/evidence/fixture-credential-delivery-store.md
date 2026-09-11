# Fixture credential delivery store path

This slice implements the store APIs for the previously documented cohesive
StartPe preparation/delivery/exposure boundary. It is fixture-only and has no
production Proxmox transport or callback authentication.

`Scheduler::with_fixture_credential_delivery` explicitly enables claims for
origins whose immutable registration policy requires a sink. Ordinary physical
dispatch still refuses such origins under the authority/run locks, including
when called on this scheduler. The older standalone credential renewal API also
refuses these origins.

`arm_fixture_start_pe` reuses transaction-local dispatch admission and writes the
registration deadline, materialized boot session, exact original bearer alias,
dispatch and delivery binding together. Its expiry derives from the original
registration deadline. Only a successful commit returns a closed envelope; no
physical permit is returned. Reload validates the complete binding and legal
acknowledgement/exposure timing. The database stores digest/reconstruction
metadata, never raw credentials or the signing secret.

`FixtureCredentialSink` accepts a stable sink UUID and an existing owner-only
local directory. It stores the credential in a mode-0600 private file, fsyncs it,
publishes the complete file by hard link and fsyncs the directory. Replay accepts
only exactly matching bytes. A killed writer can leave an orphan private temp
file; it cannot publish a partial credential or return an acknowledgement for
one. This spool is an explicit fixture transport, not a production guest channel.
The directory and its ancestor path must remain controlled by the local trusted
operator; this implementation does not isolate against a hostile same-user
process replacing directory ancestors.

Only that concrete sink can construct `FixtureDeliveryAck`. Its persistence
compares the entire immutable binding and preserves the first acknowledgement
timestamp on replay. `expose_fixture_start_pe_once` requires the matching ack,
current authority/lease, uncancelled run and original deadline; it inserts an
append-only exposure record. Only the successful first commit constructs a
consuming fake-PVE permit. A later call returns observation-only `None`.
Unexposed credential dispatches cannot enter normal outcome selection or Unknown
reconciliation.

`recover_fixture_start_pe` reconstructs the exact original credential and checks
its digest before redelivery. Changed signing material fails. Acknowledged state
requires no new sink delivery, and exposed state never regenerates a permit.
This supports recovery with an existing live grant, including a reopened store
handle. Commits `d236dedb` and `039bf818` additionally cover the dedicated
unacknowledged-delivery replacement-worker path after real lease expiry: the
original dispatch, attempt and deadline are retained, the old grant is fenced,
and a replacement controller resumes delivery and exposes exactly once.
Exposed expiry remains conservative `Unknown`; broader process-death coverage
for other stages and service-level recovery are still open. The
operation-controller requires explicit sink/secret configuration; no secret
defaults were added.

Focused local proof `fixture_credential_delivery_atomic_recovery_and_single_exposure`
covers the real Clone/Capacity/ConfigurePe prefix, ordinary admission refusal,
rollback after alias insertion, racing armers, missing ack, changed signing
material, reopened-handle recovery, lost sink acceptance reply and exact-byte
replay, immutable ack timestamp, stale generation refusal, racing exposure,
single fake submission/receipt persistence, and scanning generic database
payloads for the exact accepted credential and signing-secret sentinel.

The five `fixture_` durability tests passed serially on macOS (30.97 seconds).
The final focused test rerun, including the new outcome, renewal and stale
generation refusals, passed in 8.97 seconds. All 31 store compile-fail doc tests
passed. `cargo clippy -p postgres-store --all-targets --features fixture-ipc --
-D warnings`, default-feature `cargo check -p postgres-store`, formatting and
`git diff --check` passed.
Current-source Linux qualification, process-kill checkpoints beyond the tested
credential-reclaim path, bounded production sink ownership, service wiring and
production acceptance remain separate open gates.
