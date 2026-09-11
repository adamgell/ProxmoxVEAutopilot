# Targeted owned-v1 Linux recovery qualification

This sealed recovery-only lane used the bounded launcher mode introduced by
`c9e1ca5a` and the exact-source runner image built from `f92a6287`.

- Invocation: `configure_worker_death_after_` recovery tests only
- Result: 2 passed, 0 failed
- Runtime: 84.01 seconds
- Runner image: `sha256:5f5ddae5...`
- cgroup: cgroup2, 6 GiB PostgreSQL cap, 4 GiB runner cap, zero OOM events
- Qualification field: `INCOMPLETE` by harness design

The two supervised worker/recovery tests completed with direct fixture-worker
success output and no retained failure marker. This closes the targeted Linux
recovery lane for the sealed source; it does not close the broader full Linux
qualification or production-readiness gates.
