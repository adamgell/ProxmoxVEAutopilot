# Independent recovery reader evidence

The PostgreSQL fixture Clone test now starts a fresh test executable after the
dispatch and receipt are durable. The child opens its own PostgreSQL connection,
restores the operation and validated request from durable inputs, and reads the
exact accepted effect through fixture IPC. It requires the stored receipt to
match the effect receipt and requires submission sequence one. A mismatched
request digest is rejected. The parent verifies the attempt count is unchanged.

The child has a three-second execution bound and kill-on-drop cleanup. Its
helper test is ignored during normal discovery and invoked explicitly by the
owning proof. The database is the existing owned local fixture; credentials are
passed through the child environment, never printed or persisted in evidence.

Validation: the targeted PostgreSQL test and child passed on macOS, as did strict
Clippy for this test target.

This establishes independent-process reconstruction and receipt reads. It does
not establish worker A crashing after dispatch and worker B acquiring the lease
and advancing the scheduler. The current Scenario owns database/container and
grant setup in the parent address space. The remaining process takeover harness
must move actual controller dispatch into worker A, retain fixture/database
ownership in the supervisor, terminate A at an observed durable boundary, and
run worker B against the original attempt. Sixteen-stage and callback/service
compatibility remain separate gates.
