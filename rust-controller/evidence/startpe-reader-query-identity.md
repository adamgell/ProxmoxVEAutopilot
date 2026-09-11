# StartPe reader query identity

Baseline: `de139876`. A validated full publication does not mean every query
identifies a member of that publication. The subprocess StartPe mapping proof
now queries the target VM on a different node and a VM absent from the validated
inventory. Configuration, provisioning identity and power readers must reject
both. Media lookup must reject absent storage. The daemon's accepted journal
bytes remain unchanged across these rejected reads.

Validation: the exact
`full_start_pe_worker_death_preserves_exact_publication_and_ledger` test passed
with fixture-ipc, serial tests and `RUST_MIN_STACK=16777216`: 1 passed, 0 failed,
11 filtered out, 1.42 seconds. Formatting and whitespace checks pass. This adds
direct query-substitution coverage; runtime mapping code was unchanged.

The proof retains the genuine daemon request/receipt and restart flow. It does
not establish task mapping, complete evaluator collection or Setup/PostgreSQL
integration, and performs no physical stop or production mutation.
