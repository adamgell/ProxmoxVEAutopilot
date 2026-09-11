# Task 7 report — sanitized legacy jobs and observation-only plans

## Scope and authority

Implemented Task 7 only in the isolated `codex/rust-controller-design` worktree
from base `2da33c5`, then repaired every Task 7 round-one review finding against
the checked-out Python at baseline `c8ab4b2`. The repository MCP service and docs
inventory were healthy, but the September 4 Rust-controller worktree documents
were not present in the MCP index. The checked-out brief, approved compatibility,
fixture, and observe sections, progress ledger, existing `OperationKind`, canonical
event hashing API, and baseline Python/PostgreSQL job schema were therefore the
implementation authority.

Round three resumed from `3df8500`, preserving the interrupted regression edits.
The helper status check was blocked by the shell network sandbox; the configured
MCP documentation search still responded, but returned no indexed copy of the
Rust-controller foundation plan. Local approved documents and `git show
c8ab4b2:autopilot-proxmox/web/jobs.py` supplied the exact producer contract.

No production, Proxmox, Graph, Entra, tenant, external-network, Ansible, or
arbitrary-process action was performed. PostgreSQL tests use only the already-local
`postgres:16-alpine` image with `--pull=never`, Docker-assigned loopback ports,
and drop-guard cleanup. Cargo resolution and verification were offline and locked.

## Repaired contract

- `JobEnvelope` has private fields and only duplicate-aware raw JSON byte/string
  constructors. There is no public `Deserialize` or `TryFrom<serde_json::Value>`
  ingress after Serde map collapse. A compile-fail doctest pins that boundary.
- The fixture now reproduces the selected Python contract exactly:
  `test_long_sleep`, full `/app/playbooks/_test_long_sleep.yml`, string
  `args.duration`, and matching string `duration=N` argv. Normalization maps only
  that raw legacy shape to `OperationKind::SyntheticLongSleep`; the earlier
  `synthetic_long_sleep` wire spelling is rejected.
- `normalize_job` requires pending status, the exact executable/playbook/argument
  contract, one canonical decimal duration string from 0 through 20, a safe job
  ID, and no traversal. The ID is exactly a valid `YYYYMMDD` calendar date, a
  hyphen, and eight lowercase hexadecimal characters, matching baseline
  `JobManager._generate_id()`. Only that exact root ID grammar is exempt from
  generic encoded-text/IP scanning; malformed and nested IDs are still scanned.
  Unknown types, executables, playbooks, arguments, unsafe
  scalar types, and mismatches fail before a plan exists.
- Sanitization is bounded to 16 KiB, depth 8, 32 container members, 1,024-byte
  ASCII strings, and three recursive decode rounds. It recursively scans nested
  maps/arrays, bounded percent/Base64 decodings, and bounded Base64 candidates
  following `_`, `:`, and other safe delimiters. Plausible marked/delimited
  candidates and plausible whole-field candidates fail closed when malformed or
  non-UTF8, while valid decodings are scanned recursively. Short unmarked
  candidates begin at seven bytes and require uppercase, digit, padding, or
  Base64 symbol evidence; ordinary lowercase contract words remain admissible.
  Remaining plausible encodings at the three-round limit reject. It rejects
  control, non-ASCII, confusable and zero-width
  text; secret-shaped keys/values; broad tenant, directory, application, client,
  service-principal, object identity, `oid`, `tid`, and `principal_id` keys;
  and textual, mapped, percent-encoded, decimal, or hexadecimal non-loopback IPs.
- `NormalizedPlan`, `SanitizedValue`, and `PlanFingerprint` retain private
  construction. Fingerprints use `event-journal::payload_digest`, the repository's
  canonical JSON SHA-256 API. The repaired full fingerprint is
  `d53da5428e15afe31341a00657c50c6f8223b05f0aedea58cb04ad3b18693340`;
  observe output exposes only its 12-hex prefix.
- Both database and PVE endpoints are parsed before pool/client construction.
  Without explicit observe/read permission, both must be normalized literal
  loopback IPs. Host aliases, integer aliases, and non-loopback targets fail
  closed; explicit remote permission is ineffective in adapter/native modes.
- Observe acquires one PostgreSQL connection, explicitly begins a read-only
  transaction, verifies `transaction_read_only`, non-superuser status, SELECT on
  `jobs`, and absence of job-table write privileges on that same connection. It
  queries only `test_long_sleep` rows with `status='pending'`, ordered by
  `created_at ASC, id ASC`, then rolls back.
- Disposable PostgreSQL runs with `log_statement=all`. Tests take a log boundary
  after setup, parse generic `statement:` and `execute <name>:` records plus the
  SQLx parameter detail, and require the exact ordered BEGIN/verification SELECT/
  pending-job SELECT/ROLLBACK sequence on one backend PID. Unknown records, extra
  SQL, duplicate/missing/misplaced/wrong-backend parameter details, `FOR UPDATE`,
  and advisory-lock calls fail closed. All advisory functions are
  revoked from `PUBLIC` and the observer role; catalog privilege checks and an
  attempted `pg_try_advisory_lock` both prove denial.
- Observe production code has only a borrowed `PgConnection` capability and no
  process API. A runtime child-process snapshot also remains unchanged across
  observation. Row state and advisory-lock state remain unchanged, while the
  only output is compatibility plus a redacted fingerprint.
- The synthetic-only manifest retains baseline version `v2026.09.2`, baseline
  Git SHA `c8ab4b2`, contract version 1, and sanitizer version 1. Its recomputed
  fixture SHA-256 is
  `1635ef251a7bf2138e656381a564fca6c8cbf2b57af997932351f405b03d6001`.
  Both hashes changed in round three because the synthetic fixture now uses the
  baseline-shaped ID `20260904-deadbeef` instead of `synthetic-job-0001`.

## Round-one TDD evidence

Each repair began with a focused failing test before production changes:

- Baseline/API RED first failed to compile because `from_json_bytes` did not
  exist, then failed with `UnknownJobType` for the authoritative Python shape.
  GREEN added raw-only parsing and the exact Python-to-Rust synthetic mapping.
- Sanitizer bypass RED accepted the uppercase hexadecimal address
  `0XC0A80204`; the bounded decoder/address scan made it fail closed. A separate
  unpadded-Base64 bearer RED was also accepted before all four standard/URL-safe,
  padded/unpadded engines were checked; GREEN rejects it.
- Endpoint RED accepted database aliases/non-loopback targets because only the
  PVE string was checked. GREEN parses both endpoints and gates pool/client
  construction on normalized literal-loopback or explicit observe-only reads.
- Observe baseline RED reported no compatible job when fed the Python row.
  GREEN added pending eligibility and deterministic ordering while normalizing
  the selected fixture.
- The observe audit RED had only caller-maintained constants. GREEN replaced it
  with PostgreSQL server logs and a one-backend explicit read-only transaction.
- Advisory-lock RED proved the observer initially retained EXECUTE via `PUBLIC`.
  The first revoke filter also missed `pg_try_*`; the test stayed RED. GREEN
  revokes every `%advisory%` function from both principals and proves catalog and
  execution denial.
- Process-proof RED exposed the inert counter as non-evidence. GREEN removed the
  counter/capability entirely and added the structural connection-only boundary
  plus an OS child-process before/after assertion.

These round-one cycles extend the original RED/GREEN coverage for duplicate
keys, secret/identity/IP rejection, private construction, canonical hashing,
manifest self-consistency, and redacted rejection output.

## Round-two TDD evidence

- The supplied `b64_...` secret, `x_...` IPv4, and `base64:...` IPv6 probes were
  added first. RED accepted the prefixed Base64 secret. GREEN scans bounded
  delimiter candidates, recurses through a double-encoded secret, and rejects
  explicitly marked malformed and non-UTF8 candidates without changing the
  legitimate baseline job ID, path, duration, fixture, or fingerprint.
- Nested `oid`, `tid`, and `principal_id` UUID tests were added first. RED
  accepted `oid`; GREEN added the short identity aliases to recursive key checks.
- A generic prepared-execute/unknown-record/extra-SELECT parser probe was added
  before the parser existed and failed to compile. GREEN introduced a fail-closed
  parser. The live PostgreSQL test then passed using the isolated post-setup log
  slice, SQLx's actual execute and parameter-detail records, exact step order,
  and a single backend PID.

## Round-three regression evidence

- Before implementation, the normalization reachability test failed with
  `normalized job_c2VjcmV0`, demonstrating that an otherwise exact baseline
  envelope produced a plan with an encoded secret ID. The test now rejects all
  supplied short encoded ID probes at parsing or normalization. A separate safe
  `fake-job` probe must parse successfully and return `InvalidJobId` from
  `normalize_job()` itself, pinning the dedicated normalization gate.
- The baseline-shaped ID initially failed construction with
  `NonLoopbackAddress`. GREEN applies exact calendar-date/lowercase-hex validation
  only to the root identity, preserving random hexadecimal and digit-only
  suffixes without interpreting them as encoded data or integer IPs. Tests cover
  valid leap days, invalid dates and lengths, uppercase/nonhex suffixes, and the
  absence of the exemption on nested IDs.
- Direct sanitizer tests cover short `note: label_` suffixes (which do not
  accidentally match the older `x_` marker or the whole-field scan), a non-UTF8
  whole-field candidate, malformed candidates, and a four-times encoded secret
  exceeding the decode budget. An independent regression check temporarily
  restored the old 12-byte delimiter threshold while retaining the new ID and
  whole-field checks: RED failed with `accepted note: label_c2VjcmV0`. Restoring
  the short-candidate check returned the full API suite to GREEN.
- Before implementation, the server-log parser test failed at its duplicate
  `DETAIL` assertion. GREEN counts the detail exactly once after the pending-job
  SELECT on the same PID. Missing, misplaced, wrong-PID, duplicate, and unknown
  records all reject; the real PostgreSQL trace remains accepted.

## Final verification

Fresh commands completed successfully:

```text
cargo test --offline --locked --manifest-path rust-controller/Cargo.toml -p api-compat
# 18 unit tests + 2 compile-fail doc tests passed; 0 failed

cargo test --offline --locked --manifest-path rust-controller/Cargo.toml -p controller-service observe -- --test-threads=1
# 9 observe-filtered tests passed; 0 failed

cargo test --offline --locked --manifest-path rust-controller/Cargo.toml --workspace --all-features --no-fail-fast -- --test-threads=1
# 134 runtime tests + 6 compile-fail doc tests passed; 0 failed

cargo clippy --offline --locked --manifest-path rust-controller/Cargo.toml --workspace --all-targets --all-features -- -D warnings
# passed with warnings denied

cargo fmt --all --manifest-path rust-controller/Cargo.toml -- --check
git diff --check
# both passed
```

The final API suite was rerun after strengthening the delimiter-only regression
probe. Local Docker tests required scoped sandbox escalation, which was approved;
no integration test was silently skipped.

## Concerns and gates

Round-three implementation is awaiting independent review. An operator must provision the production
SELECT-only role and explicitly revoke advisory functions before any separately
approved production observe run; this task changed only a disposable test role.
Only the approved synthetic compatibility kind is recognized. Task 8 owns any
actual adapter/process lifecycle. Production deployment, PVE proof, production
database-role changes, and broader legacy job normalization remain separately
gated and were not attempted.
