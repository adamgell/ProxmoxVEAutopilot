# Owned-v1 full Linux qualification attempt

This run used the current sealed runner image
`sha256:5f5ddae5dd93381cc02d944d8ae397bb0b1a085f65623499122fb0f798b1caa6`
(`amd64/linux`, source `f92a62872b554a664df96e05d041876e7371cb8d`) and the
existing fixed PostgreSQL image. Host admission, receipt binding, private
network/cgroup/resource checks, and runner ownership checks all succeeded.

The full workload reached a terminal non-qualification result. Cargo emitted
the expected compile-fail doctest diagnostics; those cases are reported as
`... - compile fail ... ok` and are not the failure signal. Actual runtime
failures were:

- `intake_changed_digest_conflicts`: `local_linux_create_unconfirmed: ()`.
- `concurrent_result_probe_conflicts_then_rolls_back_and_releases_lock`:
  `explicit isolated database required: NotPresent`.
- `session_schema_creation_rolls_back_without_leaving_tables`:
  `explicit isolated database required: NotPresent`.
- `support::local_postgres::linux_postgres::owned_linux_matching_marker_wrong_oid_retains_database`
  in one package invocation, with `called Result::unwrap() on an Err value: ()`.

The Cargo group summaries include 73/1, 68/1, 137/0, 1/1, 65/0, 71/0,
60/0, and 1/0 pass/fail group results; the exact package/test context is in
`0026.stdout`. The runner then returned:

```json
{"invocation_only":{"exit":101,"failure":"ValueError: child group remains"},"qualification":"INCOMPLETE"}
```

The strict residual-process gate is independently evidenced by:

```text
native_fake_child_reap_unconfirmed pid=6340 cleanup_elapsed_us=539 deadline_remaining_at_start_us=0 deadline_overrun_us=244522486 kill_error=None wait_error=None
```

The container is retained and owned: runner exited `1`, `OOMKilled=false`,
image is the sealed runner digest, and PostgreSQL remains running with the
fixed image, `network=none`, and 6 GiB memory/swap. No cleanup or deletion was
performed. `0026.stdout`, `0026.stderr`, per-command JSON/stdout/stderr files,
`receipt.json`, and `state.json` are the authoritative records.

This result is not a passing Linux qualification. The next action is to rerun
the isolated database-dependent failures and the residual-child scenario with
the same image/launcher, retaining the strict child-group absence assertion;
compile-fail doctest text should not be fixed or treated as a source defect.
