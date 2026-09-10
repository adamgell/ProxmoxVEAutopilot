# Fixture inventory observation prefix

The opt-in Unix fixture now accepts a read-only `snapshot` request. The daemon
loads a bounded `inventory.json` from its supervisor-owned private directory at
startup. Missing input returns `unavailable`; malformed input stops startup.
The client supplies no expected node, VM, timestamp, or configuration values.

Version 1 contains a non-nil fixture UUID, node identity, nonzero Unix-millisecond
observation timestamp, and a complete synthetic node inventory of at most one VM
(VM ID, name, template flag, disk bytes). Empty inventory represents absence at
that observation time. It is not a current Proxmox absence assertion. Identity
strings and message length are bounded and unknown fields are rejected.

The daemon keeps the decoded startup observation in memory. Changing the seed
while it is running does not alter its replies. Restart reloads the same seed;
the subprocess test proves two restarts preserve the observation and corrupt
seed data rejects startup. Filesystem ownership remains the trust boundary.

This is a historical supervisor-seeded inventory, not the durable effect world's
current configuration. A future adapter must reconcile accepted effects and
generation/timestamps before using it for preflight. Storage, network, disks,
tags, firmware, guest-agent, task state/receipt, and checkpoint facts remain
unimplemented here. FixtureReadClient deliberately still does not implement
ProvisioningFakePort or PvePreflightReadPort. No controller acceptance or recovery
claim follows from this protocol prefix.
