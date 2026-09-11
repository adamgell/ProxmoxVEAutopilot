# Fixture shared-history provenance gate

This PoC slice adds `FixtureSharedHistoryProvenanceV1`, an opaque
operation-scoped value created only by an explicitly configured
`FixtureCheckpointClient`. `FixtureProvisioningPort` can expose the value only
when its checkpoint client and binding are present. A controller configured with
`with_shared_history_supervisor` can pass the outbox provenance gate only when
the supervisor-derived value is byte-for-byte equal to the port-derived value.

The comparison binds the operation, supervisor generation, owner, and exact
Unix-socket channel. The value has private fields and no deserializer or public
constructor, so a decoded receipt, copied stage identity, or caller assertion
cannot manufacture a positive join. Missing port configuration, missing
supervisor configuration, operation mismatch, generation/owner mismatch, and
different socket channels remain `SharedHistoryUnavailable`.

`reserve_fixture_stop_outbox` now returns the sealed provenance value on a
positive gate. This is deliberately only a provenance checkpoint: it does not
select or consume a PostgreSQL outbox row, release a barrier, send `qmstop`, or
claim a stopped-power observation. The existing database transaction and
physical-send integration remain later PoC gates.

## Verification

- `cargo test -p pve-port --all-features shared_history_provenance`: 1 passed.
- `cargo check -p operation-controller --all-features`: passed.
- `cargo clippy -p operation-controller --all-features --all-targets -- -D warnings`:
  passed.
- `cargo fmt --all`: passed.

This is a bounded Rust controller PoC slice, not the complete Ansible port,
not generic/legacy callback compatibility, and not production readiness.
Production `192.168.2.4` and the physical Proxmox environment were not changed.
