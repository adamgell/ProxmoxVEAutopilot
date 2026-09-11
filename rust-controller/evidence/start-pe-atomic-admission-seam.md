# Atomic StartPe fixture admission seam

This implements the ledger atomicity prerequisite identified in
`start-pe-admission-boundary-audit.md`. It does not enable StartPe through IPC,
the controller, or production.

## Implemented contract

`FixtureLog::record_start_transition` validates and writes one canonical framed,
checksummed and fsynced record containing the StartPe attempt, exact accepted
receipt/effect and a versioned synthetic stopped-to-running transition. The
transition embeds the exact durable ConfigurePe stopped predecessor, including
its operation/digest, stage/attempt/owner/generation, receipt digest, VM and
observation provenance. It must be the current power record and its effect must
still be the latest for that VM. Existing disk/PE state is preserved.

All structural identity, duplicate, predecessor, state and frame-size validation occurs
before invoking the authority-consumption closure. Consumption precedes the
durable synthetic effect. Failed ledger persistence poisons the live log; recovery
rejects incomplete records rather than exposing partial acceptance. A full atomic
record restores its attempt and exact accepted receipt together. Duplicate
admission after restart refuses before consuming authority.

Acceptance is not observation. This record does not insert a running observation.
The existing separately collected power-observation contract now requires the
accepted atomic transition before a running observation can be recorded, and
still enforces exact effect/receipt binding and timestamp ordering. A plain
non-atomic StartPe effect cannot establish running power. Historical Clone,
resize and ConfigurePe record formats are unchanged.

## Executable evidence and limits

The ledger tests exercise successful atomic acceptance/replay, exact receipt,
duplicate refusal without consumption, mismatched stopped predecessor, failed
authority consumption with byte-identical ledger, running observation only after
atomic acceptance, a torn-frame crash boundary, absence of acceptance at the
pre-frame boundary, and checksum-valid predecessor substitution rejection.

These are local ledger tests using explicitly supplied synthetic receipts and
accepted predecessor records. They are not a four-stage IPC/controller proof or
an OS-worker process-death proof. No success receipt is exposed by the existing
StartPe IPC route; its refusal tests remain in force.

Next: bind this private seam to a typed StartPe request and explicit power-aware
current-generation supervisor authorization. Preserve fresh stopped evidence
rather than promoting historical replay evidence. Then connect stage-bound
`qmstart` task/running observations, adapter restoration and the PostgreSQL
four-stage controller/process-death proofs. A real task receipt alone must never
be treated as a running VM observation. No real Proxmox, production/default path,
callback or `192.168.2.4` mutation was performed.

## Validation

- Fixture-feature ledger unit tests: 2 passed.
- Default-feature `fixture_daemon durable_fixture_log::power_tests`: 2 passed.
- Fixture-feature post-dispatch/stage/stage-consumer suites: 15 passed.
- Fixture-feature daemon suite with `--test-threads=1`: 21 passed, one intentional
  child entrypoint ignored.
- Strict fixture-feature all-target Clippy passed for `pve-port`.
- Formatting and diff checks passed.

The initial parallel daemon-suite run had one `ConnectionRefused` failure in
`daemon_lifetime_keeps_a_sixty_second_hard_limit` at its initial socket connection;
the other 20 tests passed. The lifetime test passed on immediate isolated rerun,
then the full suite passed serially. This suggests a socket-startup timing race,
but no unrelated timing code was changed or claimed fixed. No PostgreSQL or
controller process-death matrix was rerun for this ledger-only seam.
