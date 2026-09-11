# Export integrity and rollback-window evidence gate

The additive artifact-index contract retains exact SHA-256 identities for source,
image, proof bundle, rollback image and rollback proof exports. It recomputes
hashes over supplied nonempty bytes; an original immutable declaration must match
exactly, including the original microsecond rollback window. Substituted exports
or extended windows fail. At the deadline the window is closed; even before it,
matching bytes return cutover-and-rollback-authority-unavailable.

This is byte integrity only, not semantic proof validation, a container image
manifest digest, source commit attestation, signing verification, backup restore
qualification or proof that rollback can safely reverse database changes. Window
and original declaration provenance require independent trusted records. No
filesystem export, deployment, rollback action, production write, approval or
cutover capability is implemented. Expiry never triggers an automatic action.

Validation: focused test mutates all five supplied artifacts, substitutes the
window, checks exact expiry/pre-window times and rejects malformed declarations;
artifact-index suite, strict artifact-index Clippy, formatting and diff checks.
Live export/restore rehearsal and production rollback qualification remain open.
