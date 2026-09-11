# Frozen-source Linux build and focused StartPe admission

The Git archive at `518878d7a4c10ddccd2d21656caa7fa3a1308aeb` was built
with `rust-controller/Dockerfile.test`, `--platform linux/amd64`, and the
matching `CONTROLLER_GIT_SHA` build argument. The context contained only
`rust-controller` and `autopilot-proxmox/playbooks/_test_long_sleep.yml` from
that commit. Concurrent working-tree content was excluded.

The bounded build passed in 1072.76 seconds, exit 0, with the direct child
and process group reaped. Release compilation, all-feature test compilation,
and every Dockerfile focused test layer passed. `build.log` has SHA-256
`f7ce8086974c9e0ab1bb10d23fe6c1a987adedc21803355002e18d45c42a5255`.
The inspected AMD64 image ID is
`sha256:55564eb90ccd5f3773a8dbaf91b2c2e7821770972e376e21a077a60db10e668e`.
Its source environment matches the archive commit.

Launcher commit `27b69e2b` adds a closed `--mode start-pe` command selecting
only `fixture_start_pe_atomic_arming_rollback_race_and_reload` with
`--features fixture-ipc --test osdeploy_durability --exact`. Its child bound
is 300 seconds, and output must name that test and report exactly one pass.
All 14 launcher unit tests passed. The source and image pins are immutable.
The first invocation was refused before resources because the launcher edit
was not yet committed; receipts remain in sibling directory
`requalification-518878d7-start-pe-1`.

The second invocation passed the runtime-source and image checks, then
failed the unchanged VM-memory admission guard before runner creation or
Rust test execution. Receipt `0023` records `MemAvailable: 12058844 kB`,
approximately 11.50 GiB, below the required 12 GiB. The admitted PostgreSQL
fixture is `2c0a7f5cbcb15f73b583b72b57effa09758cd1dafc62e7968f6dacaca7e75cd9`,
session `6986e6a7eae94f038dcad20196d153b6`; it has a 6 GiB cgroup cap,
4 GiB tmpfs, and zero OOM events. It remains retained. No runner was created
by this invocation. Do not retry by accumulating more fixtures or lowering
the resource guard.

Read-only inspection also resolved the previous pending runner from source
`35400bd0`: `c04a1e32dc1921eae947cfca2cae5228acff24eb1ce8039c7b425b741507f7a1`
is in `created` state, `Running: false`, PID zero, with no start time.
The exact response and bounded receipt are retained as `prior-runner.*`.
It was not started, stopped, deleted, or otherwise mutated.

The build is successful Linux compilation and focused-test evidence. The
database-backed StartPe proof remains unexecuted on Linux, and full Linux
qualification remains incomplete. No production system was modified.
