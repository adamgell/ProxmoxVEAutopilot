# StartPe full readback identity gate

The attempted general-adapter read-only validation slice is not implemented.
Its full-postcondition test failed `FixturePostDispatchV1::decode_stage` with
`invalid fixture Clone reads`; the incomplete implementation was removed.

The existing inventory wire contract names its configuration identity
`config_sha256` and requires exactly 64 lowercase hexadecimal characters.
The provisioning identity projection copies the configuration's opaque
`digest()` unchanged. The fresh fixture chain uses `clone-digest` and
`after-ConfigurePe`; these cannot be relabeled as SHA-256 evidence. A focused
regression now pins the legacy schema's acceptance of a correctly shaped digest
and refusal of these opaque values. This is schema evidence, not proof that
any syntactically valid hash is authentic.

Next implementation gate: introduce an additive, versioned full-postcondition
inventory contract that explicitly distinguishes opaque PVE configuration
digests from canonical hashes, preserves exact observed identity, and binds
configuration, inventory, media, task, and power evidence to the accepted
StartPe operation/attempt/generation/owner/request and original receipt.
Persist or independently re-observe those complete postconditions before
general adapter restoration. Do not manufacture a digest to satisfy v1.

The previously committed typed task/running restoration remains intact.
No generic adapter installation, controller satisfaction, real qmstart,
production path, or real Proxmox change is included in this slice.

Validation: the new legacy-inventory regression passed (1 test), and the
existing real fixture IPC synchronous-ConfigurePe/StartPe publication and
restart proof passed (1 test). Strict Clippy for pve-port and
operation-controller with all targets and fixture-ipc passed, as did workspace
format checking and git diff whitespace checking. No PostgreSQL integration
suite or production-readiness acceptance is claimed.
