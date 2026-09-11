# StartPe session preparation: concrete implementation decisions

Source inspection at `a755abd8`. This is an implementation design input, not a
session implementation or proof of callback compatibility. No schema, controller
default, or dispatch admission changes are made by this document.

## Legacy identities that must not be conflated

`autopilot-proxmox/web/winpe_token.py::sign` issues a stateless HMAC credential
containing only `run_id` and integer `exp`. Issuing twice for the same run in the
same second with the same TTL produces the same credential. Verification cannot
distinguish sessions, agents, roles, VM identities, or attempts. Rust's verified
run-bearer type preserves that limited meaning.

`web/winpe_endpoints.py::post_register` resolves an integer provisioning run from
the supplied VM UUID and `awaiting_winpe` state, then issues a one-hour token.
`web/osd_v2_endpoints.py::register_agent` resolves the supplied string run ID and
issues a 24-hour token. The v2 registration handler does not require an existing
bearer. These are distinct registration surfaces and cannot share an inferred
VM-authentication guarantee.

`web/osdeploy_endpoints.py` builds the full-OS agent package with a server role,
agent ID, run ID, VM ID and bootstrap URL. The bootstrap credential is still the
run token. `web/agent_v1_endpoints.py::_bootstrap_run_id` also accepts the separately
configured fleet bootstrap credential. Neither the package's role field nor the
fleet fallback is a PE-session witness. Preserve the distinction between PE
registration and persistent-agent bootstrap when wiring the Rust service.

## Proposed private durable representation

The following field mapping is concrete enough to implement once credential
issuance and registration selection below are settled. It must live behind
store-private constructors; deserialization must never construct an armed permit.

| Field | Authoritative source and invariant |
| --- | --- |
| session_id | Server-generated public correlation UUID, globally unique; never authentication by itself |
| run_id, start_operation_id, registration_operation_id | Registered immutable workflow; verify both stages belong to the same run |
| attempt_id | Current StartPe logical attempt under scheduler locks; unchanged across recovery |
| workflow_sha256, operation_plan_sha256, request_sha256 | Reconstructed registered plan and exact request admitted by dispatch |
| role, label, package_identity | Server-prepared immutable package identity, never copied from callback claims |
| vm_identity | Registered/resolved owned VM identity with its original ownership evidence |
| prepared_event_id | Durable preparation event, separate from arming/dispatch |
| dispatch_event_id, opened_at, budget_seconds, deadline_at | Same original dispatch event and microsecond timestamp that anchors pe_registration |
| original_generation | Existing PostgreSQL bigint authority generation; not the UUID in the earlier proposal context |
| worker_id, lease_acquisition_event_id | Existing current grant and durable lease epoch checked in the dispatch transaction |
| credential_binding | Requires the explicit issuer/renewal choice below; never raw bearer text in logs or public projections |

Preparation should persist only immutable package/session intent. Arming is a
separate private transition in the existing dispatch transaction. A prepared row
must grant no callback completion or physical dispatch authority.

## Decisions required before creating a success-capable row

1. **Credential correlation:** retaining only legacy `run_id,exp` wire claims
   requires a server-side issuance registry or another existing authenticated
   request witness to associate a credential with one session. A digest unique
   constraint alone is insufficient because legacy deterministic issuance can
   yield identical tokens. Decide whether duplicate issuance belongs to the same
   session, whether a run has one eligible session, and how renewal preserves
   original session identity. Adding a session/nonce claim changes the canonical
   signed format and needs an explicit compatibility path.
2. **Registration selection:** specify which of the integer WinPE and string v2
   registration surfaces the native controller supports first, and how its
   supplied identity maps to registered VM/run/package identity. Neither handler
   currently proves possession of a unique VM credential. Bootstrap observation
   plus subsequent run bearer must remain separate recorded witnesses.
3. **Role/package provenance:** select the immutable source record and exact
   label semantics for the PE package. The full-OS persistent-agent package is
   not an interchangeable source. Pin its bytes/digest and provisioning moment.
4. **Renewal/revocation:** define whether old credentials remain admissible after
   renewal, cancellation, replacement session, and authority handoff. Preserve
   observed receipt evidence separately from current continuation authority.

These choices are not recoverable from the existing token verifier or a generic
UUID session proposal. Adding a table with guessed values would conceal the
missing contract rather than settle it.

## Exact transaction integration and tests

Integrate arming in
`crates/postgres-store/src/scheduler/osdeploy/pve.rs::begin_osdeploy_pve_dispatch`.
Keep its current authority, run/operation and lease lock ordering. Reconstruct the
request and package/session binding under those locks, create the dispatch event,
and atomically insert the armed linkage with dispatch and original registration
anchor. Validate the final lease/deadline before commit. Only the existing
post-commit return may release a one-shot dispatch permit.

Use independent owned PostgreSQL pools for race tests. Assert rollback after
preparation validation, event insertion, session arming and dispatch insertion;
none may leave an armed session without its matching dispatch/deadline. Race two
armers and require one immutable linkage. After commit/response loss, reload the
same session/event/attempt/deadline and refuse a second physical-start permit.
Reject substitution of each field above and stale generation, lease epoch,
worker or revision. Test equal-deadline expiry without rounding microseconds.
Then test duplicate credential issuance, renewal and duplicate/conflicting
registration according to the selected contract before enabling StartPe.

Validation for this artifact: inspected the named Python handlers, token issuer,
accepted transaction-boundary/main-decisions notes, current session refusal seam,
and scheduler dispatch source. No runtime tests were needed for this document;
all persistence and callback scenarios listed above remain unimplemented.
