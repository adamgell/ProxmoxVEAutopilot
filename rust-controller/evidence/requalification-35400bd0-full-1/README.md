# Linux build and owned runner admission

The exact Git archive at `35400bd02b9816b3a0bdff0c7df3cfd20fd6d1ee`
was piped into `rust-controller/Dockerfile.test` with the matching
`CONTROLLER_GIT_SHA` build argument. The context included `rust-controller`
and `autopilot-proxmox/playbooks/_test_long_sleep.yml`; concurrent working-tree
content was excluded.

The build completed successfully in 826.80 seconds, exit 0, with the watchdog
confirming the child and process group were reaped. This includes release
compilation, all-feature test compilation, and the Dockerfile's focused test
layers. The retained `build.log` has SHA-256
`43bf5dd85822991169ed9c2630a6c703187afd0536e0b9091e07fd963ffae18e`.

The inspected Linux AMD64 image is
`sha256:d615e6c8b3270ae13dc9ca95cf49aea549192b6775c1d0f1aa711457f5927f94`.
Its source environment matches the archive commit. Launcher commit `8ac7856f`
pins that source and image; all 13 launcher unit tests passed. Runtime paths
were unchanged from the source commit at admission.

The full owned lane exited 1 during runner creation, before controller test
execution. Receipt `0024.json` records the unchanged 60-second create bound,
`exit: -9`, and `ValueError: child deadline`. PostgreSQL was admitted with
cgroup2, a 6 GiB memory cap, a 4 GiB tmpfs, and zero OOM events. The runner
identity remains pending in `state.json`; its exact reserved name is
`task9-039b11b7013e4c5c9662555e7c812fa8-runner`.

Do not count this admission failure as a Rust test failure or a full Linux
qualification pass. No resources were deleted, and no production system was
changed. Inspect the exact pending container name before resuming any work
on that resource. The first invocation using the descriptive image tag was
refused before resource creation; the verified image was then assigned the
launcher's required `rust-controller-task9:local` tag.

Follow-up read-only diagnosis could not resolve the pending identity: the
exact-name `docker ps -a --no-trunc --filter name=...` remained live beyond
two minutes, and a direct Docker socket GET for that container's JSON timed
out after 10.002 seconds with zero response bytes (curl exit 28). This is
insufficient evidence of absence or terminal state. No retry was started.
