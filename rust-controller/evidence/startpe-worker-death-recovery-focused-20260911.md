# StartPe worker-death and publication recovery proof

At current source `5c60976e7385aaeb097c9d6e2bab477220d7797c`, Astra ran the
genuine fixture daemon recovery test with `fixture-ipc` enabled:

```text
RUST_MIN_STACK=16777216 RUST_TEST_THREADS=1 cargo test --locked \
  -p pve-port --features fixture-ipc --test fixture_post_dispatch \
  full_start_pe_worker_death_preserves_exact_publication_and_ledger \
  -- --exact --nocapture --test-threads=1
```

Result: `1 passed; 0 failed` (`11 filtered out`) in 1.41 seconds. The test
exercises accepted Clone, DiskCapacity, ConfigurePe, and StartPe publication,
worker death, exact durable publication/ledger preservation, restart
restoration, identity fencing, malformed-persistence refusal, and full
readback validation. It uses the local fixture daemon and does not use
synthetic PostgreSQL outcome rows or production/real-Proxmox access.

This strengthens process-death recovery evidence but does not close the
connected PostgreSQL StartPe-to-stop outcome matrix or establish production
readiness.
