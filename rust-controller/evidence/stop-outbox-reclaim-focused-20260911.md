# Stop outbox reclaim focused proof

At source revision `e62f1fd6f587ff7d1072c4d78a087687011f5fbe`, the local
PostgreSQL store test for consumer-crash reclaim passed:

```text
RUST_MIN_STACK=16777216 RUST_TEST_THREADS=1 cargo test --locked \
  -p postgres-store --test postgres \
  expired_outbox_claim_is_reclaimed_by_a_second_connection_after_consumer_crash \
  -- --exact --nocapture --test-threads=1
```

Result: `1 passed; 0 failed` in 1.59 seconds. This proves an expired stop
outbox claim can be reclaimed by a second connection after consumer crash in
the local fixture store. It does not prove physical stop execution or the
connected accepted/refused/ambiguous outcome matrix.
