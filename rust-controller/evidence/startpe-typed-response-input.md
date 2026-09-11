# Closed fixture response transaction input

`OsDeployResponseCapture::bind_fixture_start_pe_response` now composes the
original committed dispatch capture with a closed `FixtureStartPeResponseV1`
created by successful IPC submission. Both values are borrowed; neither is
recreated from caller-supplied IDs or JSON. The resulting
`FixtureStartPeCaptureInput` has private fields and cannot convert into a
dispatch permit.

Composition validates the captured request's run, operation, attempt, complete
request digest, workflow fingerprint, operation-plan fingerprint and StartPe
action against the original dispatch identity. It validates both fixture stage
identities and the original ConfigurePe predecessor relationship, requires the
same fixture UUID across predecessor and StartPe, and derives the semantic
receipt by decoding the exact captured envelope. The original bigint scheduler
generation is not equated with the independent fixture supervisor UUID.

This is checked input for a future locked write. It does not access SQL, commit,
create a receipt journal event, renew a lease, grant current authority or send.
It also does not prove that a fixture UUID/owner is the controller's configured
route: the store's original capture currently has no such routing provenance.
The future integration must join trusted operation-specific fixture routing and
revalidate persisted dispatch/session identities under the existing lock order.

No atomic persistence method is added in this change. The next integration
still needs a genuine StartPe IPC dispatch with a store-issued capture and
atomic storage of semantic receipt plus original envelope. Until then, no
rollback/replay/restart SQL result is claimed. The input's public accessors are
observation data only; there is no general constructor that bypasses either
closed source value.

Verification includes three compile-fail cases: JSON construction, public-field
construction and conversion to a dispatch permit. Feature-enabled all-target
compilation is verified. Runtime identity-substitution and database atomicity
proofs remain requirements of the connected integration, rather than being
simulated by fabricating either source capability in a test.

Results: 3/3 compile-fail tests passed; all-target feature-enabled compilation,
all-target feature-enabled Clippy with warnings denied, default-feature library
compilation, formatting and targeted whitespace checks passed.
