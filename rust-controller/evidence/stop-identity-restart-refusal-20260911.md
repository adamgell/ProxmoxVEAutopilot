# Stop identity restart/refusal proof

At current source revision `01d869b6ec1c1365fc1a203263bc5d9f21e42e9c`, the
fixture stop consumer was tested with all features enabled:

```text
RUST_MIN_STACK=16777216 RUST_TEST_THREADS=1 cargo test --locked -p pve-port \
  --all-features --test fixture_stage_consumers \
  stop_identity_cannot_acquire_disk_only_release_or_effect_across_daemon_restart \
  -- --exact --nocapture --test-threads=1
```

Result: `1 passed; 0 failed` in 2.10 seconds. The proof confirms that a stop
identity cannot acquire a disk-only release or physical effect across daemon
restart. It is fixture safety evidence only; physical stop dispatch and the
authentic accepted/refused/ambiguous PostgreSQL outcome matrix remain open.
