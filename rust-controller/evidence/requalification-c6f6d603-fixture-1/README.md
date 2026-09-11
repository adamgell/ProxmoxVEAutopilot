# Current-source Linux fixture qualification attempt

This attempt was run on 2026-09-11 from the isolated macOS worktree. It used
an exact Git archive of source `a607861f24cac19cad4565365df2a29b58a33e0b` and
the immutable image `sha256:7178b72fa5b4d1f8cc1fc1ecf5f6896909f2a3e7eead32733e4a6be450eaaef9`.
The image metadata reports `linux/amd64` and the matching
`CONTROLLER_GIT_SHA`.

The image build completed successfully, including release compilation,
all-feature test compilation, workspace tests, fixture-IPC tests, and
controller-service tests. The owned fixture launcher then refused admission at
the host memory guard before starting the runner: observed host
`MemAvailable` was `11,588,404 kB`, below the required 12 GiB threshold.
The PostgreSQL container was retained, and its cgroup showed no OOM or memory
pressure events. The retained `state.json` and numbered command receipts are
the authoritative failure record.

This proves exact-source image construction and test compilation only. It does
not qualify the Linux owned runtime, and it does not support production
readiness, deployment, cutover, or Ansible retirement. No production
controller, `192.168.2.4`, real Proxmox state, or external infrastructure was
mutated.
