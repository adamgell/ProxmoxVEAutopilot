# PeRegister rollback/concurrency local SQL proof

The opt-in `concurrent_result_probe_conflicts_then_rolls_back_and_releases_lock`
test passed at source HEAD `e8846c993f4c1d9162e003ad6f03cccec05a2334` (implementation
`4ed4a23b`), with 1 passed, 0 failed in 0.07 seconds. It proves advisory-lock
contention returns Conflict, rollback releases the lock, an uncontended probe
returns CapabilityUnavailable, and no `rust_controller` schema is created.

The Compose fixture does not publish PostgreSQL port 5432 to the macOS host.
Instead, this proof used the existing native harness shape: a newly owned
PostgreSQL container, loopback-only ephemeral 5432 publication and 1 GiB PGDATA
tmpfs. No existing Compose database or volume was reused.

Owned container:
`9333dcd8a0e78fe5104a9a18db4b9b63ac2dc5141cffbf1ac1fcd6f0b8263b40`
(`native-proof-pe-register-4ed4a23b`). Its exact ownership label, image digest
`sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777`,
tmpfs configuration, empty mounts and `127.0.0.1:36577` mapping were inspected.
The explicitly supplied database was `native_test`. Test and inspection receipts
are retained under `pe-register-4ed4a23b-local/`.

An independent post-test SQL read reported the expected database,
`rust_controller` namespace absent, and zero advisory locks. Final inspection
reported running, OOMKilled false and zero restarts. The owned container and its
tmpfs database are retained; no deletion, stop, volume cleanup or engine restart
was performed. Production and `192.168.2.4` were untouched.

Executed from `rust-controller`:

```sh
PVA_START_PE_SCHEMA_TEST_DSN=postgresql://postgres:postgres@127.0.0.1:36577/native_test RUST_MIN_STACK=16777216 cargo test --offline --locked -p postgres-store --features fixture-ipc --test pe_register_result_transaction concurrent_result_probe_conflicts_then_rolls_back_and_releases_lock -- --exact --ignored --nocapture --test-threads=1
```

The credentials above belong only to this newly created local test database.
This proves the rollback-only probe, not PeRegister result acceptance or a
production-ready session lifecycle. Test source SHA-256:
`675711afb5da708c7cc63ddfba5701298ea24053e7c01f96658dc060e8a72338`.
