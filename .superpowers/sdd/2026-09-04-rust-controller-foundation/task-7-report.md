# Task 7 report — sanitized legacy jobs and observation-only plans

## Scope and authority

Implemented Task 7 only in the isolated `codex/rust-controller-design` worktree
from base `2da33c5`. The repository MCP service and docs inventory were healthy,
but the September 4 Rust-controller worktree documents were not present in the
MCP index. The checked-out Task 7 brief, approved compatibility/fixture/observe
spec sections, plan, progress ledger, existing `OperationKind`, canonical event
hashing API, and Python PostgreSQL job schema were therefore the implementation
authority.

No production, Proxmox, Graph, Entra, tenant, external-network, Ansible, or
arbitrary-process action was performed. The PostgreSQL tests use only the
already-local `postgres:16-alpine` image with `--pull=never`, Docker-assigned
loopback ports, and drop-guard cleanup. Cargo resolution and verification were
offline and locked.

## Contract implemented

- `JobEnvelope` is a private-field validated wire boundary for the selected
  legacy job projection (`id`, `job_type`, `playbook`, `cmd`, `args`, and
  `status`). Both `JobEnvelope::from_json_str` and its public Serde ingress use
  a recursive visitor that detects duplicate object keys before a Serde JSON
  map can collapse them. Unknown envelope fields are denied.
- Sanitization recursively rejects token, password, secret, bearer, and private
  key-shaped keys or values; tenant/application/client/app UUID fields; and
  non-loopback IPv4 or IPv6 address values. The fixture contains synthetic data
  only.
- `normalize_job` recognizes only `synthetic_long_sleep`. It requires the exact
  `ansible-playbook`, `_test_long_sleep.yml`, `-e`, and `sleep_seconds` contract,
  a pending job, a safe job ID, matching structured/argv values, an integer from
  0 through 20, and no path traversal. Unknown types, executables, playbooks,
  arguments, unsafe scalar types, and mismatches fail before a plan exists.
- `NormalizedPlan` and `SanitizedValue` have private construction boundaries.
  The plan consumes the existing closed `OperationKind::SyntheticLongSleep`
  domain value and exposes read-only accessors. A compile-fail doc test proves
  downstream callers cannot use public field construction to bypass
  normalization.
- `PlanFingerprint` reuses `event-journal::payload_digest`, so its full value is
  lowercase SHA-256 over the repository's canonical JSON policy. Observe-mode
  output exposes only a 12-hex redacted prefix.
- Controller observe mode first verifies the configured PostgreSQL role has
  `default_transaction_read_only=on`, then issues one fixed parameterized job
  `SELECT`. Its statement audit records exactly those two SELECTs. It does not
  claim rows, take `FOR UPDATE` or advisory locks, write state, invoke a PVE
  port, or possess a process-spawn capability.
- Compatible output is exactly a compatibility result plus redacted
  fingerprint. Rejected payloads return only `rejected` plus `[redacted]`, so
  unsafe job material is not reflected.
- `fixtures/manifest.json` records synthetic fixture name, baseline version
  `v2026.09.2`, baseline Git SHA `c8ab4b2`, contract version 1, sanitizer version
  1, and the fixture's raw SHA-256. A test recomputes the hash and then runs the
  checked-in fixture through sanitization and normalization.

## TDD evidence

The sanitizer tests were written before `job.rs`. The initial offline RED failed
because the module and API did not exist. The minimal implementation then made
the duplicate-key, secret pattern, tenant/application UUID, IP-address, and
Serde-ingress tests pass. Follow-up RED cases proved that OpenSSH private-key
markers and `tenant_uuid`/`application_uuid` fields were initially accepted;
the expanded sanitizer made the same focused tests green.

Normalization tests preceded `plan.rs`. Their RED failed because the plan module
did not exist. GREEN established the closed synthetic contract and the
hand-derived canonical fingerprint
`e7cfdb9b71c50b5dc87680c88e9412b3d00cabe94ea89ffa72f6c7c49acd1fee`.

The fixture-manifest test preceded both fixture files. Its RED reached the
intended missing-file assertion. After adding the synthetic fixture and manifest,
the same test recomputed
`9deb5ed55d334a8cd7f9c21d81e11c4de095ba3ca42700bc6c467aa592408b06`
and passed.

The observation integration tests preceded the observe implementation. Their
RED failed on missing `ObservationAudit` and `observe_once`. The first GREEN used
a role granted SELECT but no INSERT/UPDATE/DELETE privilege, compared the job row
before and after, compared advisory-lock counts, audited the exact statements,
and checked redacted output. A later role-gate RED showed an admin/read-write
connection was accepted; the implementation now verifies PostgreSQL's default
read-only setting before reading a job. A final rejection-output RED initially
returned an error chain for a token-shaped payload; GREEN now emits only the
sanitized rejected result.

## Final verification

Fresh commands completed successfully:

```text
cargo test --offline --locked --manifest-path rust-controller/Cargo.toml -p api-compat
# 10 unit tests + 1 compile-fail doc test passed; 0 failed

cargo test --offline --locked --manifest-path rust-controller/Cargo.toml -p controller-service observe
# 4 observe-related tests passed; 0 failed

cargo test --offline --locked --manifest-path rust-controller/Cargo.toml --workspace --all-features --no-fail-fast -- --test-threads=1
# 120 runtime tests + 5 compile-fail doc tests passed; 0 failed

cargo clippy --offline --locked --manifest-path rust-controller/Cargo.toml --workspace --all-targets --all-features -- -D warnings
# passed with warnings denied

cargo fmt --all --manifest-path rust-controller/Cargo.toml -- --check
git diff --check
# both passed
```

## Concerns and gates

No blocking Task 7 concern remains. The observe process requires an operator-
provisioned PostgreSQL role whose default transactions are read-only; role
creation or production configuration is intentionally not part of this task.
Only the approved synthetic compatibility kind is recognized. Task 8 owns any
actual adapter/process lifecycle. Production deployment, PVE proof, database
role changes, and broader legacy job normalization remain separately gated and
were not attempted.
