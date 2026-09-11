# Targeted recovery retry: Docker unavailable

Command run from the Rust workspace:

```text
RUST_MIN_STACK=16777216 cargo test --offline --locked -p operation-controller --features fixture-ipc --test postgres_fixture_clone configure_worker_death_after_ -- --nocapture --test-threads=1
```

Both selected tests failed before controller recovery execution because the
owned PostgreSQL fixture could not start:

```text
local_database_unavailable: local_process_timeout
```

The Docker API also exceeded a four-second read-only `docker version` bound in
the same session. This is an environment/fixture admission failure, not a
controller assertion or recovery result. No source or production state changed.
