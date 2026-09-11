# Legacy bearer renewal and durable session identity

Source reviewed: `dc703de4215e7dccf8b82938c01ab2822814b11b`.
This is a concrete implementation recommendation. It does not enable StartPe,
create an authenticated witness, or claim that the persistence tests below pass.

## Recommended compatibility rule

Keep the canonical legacy bearer wire format. A server-created session belongs
to one immutable `(run, StartPe operation, logical attempt)` binding. Reissuing a
credential for that binding renews access to the same session. Issuance does not
create a session, change its attempt, reset its original registration deadline,
or reopen a terminal session. Process restart and worker lease replacement also
do not create sessions.

The legacy issuer is deterministic for a given run and expiration. This is not
itself a defect: `winpe_endpoints.py::post_step_status` and the v2 next/status
responses intentionally issue replacement credentials during the same run.
`post_register` explicitly reuses existing steps on re-registration. These
behaviors support session continuity rather than one session per token.

Persist credential associations before publishing tokens. An association names
one session and remains immutable, including after that session terminates.
Duplicate association with the same session is idempotent; association with a
different session conflicts. Use verified canonical claims plus issuer/key
identity for association identity, rather than relying on arbitrary textual
encodings of a signature. Never retain raw tokens in evidence or projections.
Each callback must still validate signature, expiry, run, association, session
state, expected callback role, and the current durable authorization fences.

Old and renewed credentials may coexist until their individual expiry while the
same session remains eligible. Renewal does not revoke the prior credential:
the Python verifier is stateless and has no such behavior. Cancellation or
termination closes continuation for every credential of that session, without
erasing receipts or granting an old callback new authority after a restart.

For a new logical attempt, never resolve a credential by simply selecting the
run's newest active session. An identical association must fail with an explicit
identity conflict. A replacement workflow may use a new run ID; if retaining the
same run across independently authenticated sessions is required, implement an
explicit versioned session/nonce credential path. Waiting for a different second
is not an identity protocol. The initial compatible path must report this
limitation rather than silently reuse an old session or weaken attempt fencing.

## Authentication boundary

This rule resolves renewal semantics; it cannot prove which VM supplied a
callback. The current integer WinPE registration endpoint selects by caller-
supplied UUID and state. The v2 registration endpoint accepts caller-supplied
run ID, agent ID and phase without an existing bearer; its tests exercise this
unauthenticated issuance. These are observed compatibility behaviors, not proof
of exclusive VM possession.

A Rust session may be prepared from trusted server plan/package identity before
boot, and StartPe may be armed atomically under scheduler authority, but neither
fact turns an unauthenticated registration request into a VM-authenticated
completion witness. A callback path requiring that stronger witness needs an
independent package/bootstrap credential or versioned challenge contract. Keep
that requirement separate from renewal and keep registration completion closed
until it is implemented. A new nonce alone also does not authenticate the VM if
an unauthenticated registration endpoint gives it to any caller naming the run.

## Persistence acceptance cases

1. Same run/attempt and equal expiration: duplicate issuance returns the same
   association without creating a second session or dispatch.
2. Same session and later expiration: both credentials resolve to the same
   session; the original registration deadline remains byte-for-byte unchanged.
3. Token from the old session after replacement: never maps to the replacement,
   even if the old session is terminal and the replacement is the only active row.
4. Two transactions associate the same credential with different sessions:
   exactly one wins; the other rolls back with an identity conflict.
5. Session cancellation, expired lease or stale generation: renewal and callback
   continuation refuse; historical receipt inspection remains possible.
6. Crash before association commit: no token is published. Response loss after
   commit: retry returns the existing immutable association.
7. Caller-supplied VM, role, phase or agent fields disagree with trusted package
   identity: refuse completion even when the run bearer verifies.
8. Bearer remains cryptographically valid at its integer expiry boundary while
   the microsecond registration deadline has elapsed: deadline refusal wins.

## Source evidence

- `autopilot-proxmox/web/winpe_token.py:56`: canonical run/expiration signing;
  verifier performs signature/expiry validation without session storage.
- `autopilot-proxmox/web/winpe_endpoints.py:109`: UUID/state selection and
  re-registration reuses existing steps; `:341` renews after step status.
- `autopilot-proxmox/web/osd_v2_endpoints.py:602`: registration has no bearer
  dependency; `:628` next-action processing requires a run bearer and renews it.
- `autopilot-proxmox/tests/test_osd_v2_endpoints.py:231`: package compatibility
  test obtains a token by posting run ID, agent ID and phase without a bearer.

Validation: direct source inspection of the named paths. This document selects
a conservative rule that can guide implementation; it adds no runtime behavior.
