# Local macOS all-feature qualification

Run date: 2026-09-11

Command:

```text
RUST_MIN_STACK=16777216 RUST_TEST_THREADS=1 cargo test --locked --workspace --all-features
```

The complete workspace run terminated successfully. Notable terminal results
included:

- OSDeploy durability: 142 passed, 0 failed, 0 ignored.
- PostgreSQL OSDeploy: 68 passed, 0 failed.
- PostgreSQL scheduler/native matrix: 62 passed, 0 failed.
- Callback compatibility: 5 passed, 0 failed.
- Fixture post-dispatch: 12 passed, 0 failed.
- Controller-service, artifact, domain, adapter, visibility, provisioning,
  recovery, and doctest suites completed without failures.

This is local macOS evidence for the Rust workspace and its bounded fixture and
native proofs. It does not qualify the owned Linux runtime, production
deployment, Ansible retirement, or cutover. Production `192.168.2.4`, the
existing controller, and real Proxmox remained read-only.
