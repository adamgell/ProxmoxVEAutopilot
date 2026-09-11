# PeRegister atomic result boundary (capability closed)

The default-disabled fixture feature exposes a source-only PostgreSQL caller
contract built on verifier descriptor `47d13f24`. Its only disposition is
`RollbackRequired(reason)`. It cannot connect, insert, authenticate, return a
commit permit, or translate Duplicate into a successful callback response.
Inputs are diagnostic descriptors, not trusted database observations.

Required future atomic transaction:

1. Acquire the established authority/run/operation lock order. Reconstruct the
   bigint authority generation, worker identity, lease epoch, operation revision,
   session/request binding and expected result revision from durable records.
2. Verify authenticated witness, cancellation and lease validity, original
   dispatch anchor and registration deadline using the database clock. Reload
   the original result under the same locks; do not trust callback identities
   or a descriptor supplied outside the transaction as authority.
3. Compare the immutable result identity and payload before any insertion.
   A stale fence, different payload/revision, invalid binding, or expired budget
   must abandon all tentative writes. Duplicate handling must not extend grace,
   replace the original result, append another selection, or return success
   without authentication.
4. Atomically insert the selected result/journal fact, apply revision CAS, and
   perform the specified registration/session/grace transition. Revalidate the
   fence before commit. Any failure rolls back the entire unit, never just the
   result row. Commit-to-response crash recovery must use the original identity.

None of those database writes, locking or witness implementations is supplied by
this slice. No schema or production path changes. Local tests use an unreachable
lazy pool and cover unavailable witness, duplicate, result-revision conflict,
expiry, and stale fence winning over a duplicate. They prove no-connection
refusal, not an executed SQL rollback. SQL rollback, concurrent CAS, authenticated
replay and commit/response crash tests remain explicit isolated-runtime gates.
