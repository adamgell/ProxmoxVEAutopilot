# Frozen e0bb5f10 runtime preflight: memory guard

After the successful frozen-source build of
`e0bb5f10f66ee1084a30a92a127b5cefe6551294`, runtime preparation checked the
existing VM's available memory before allocating another database fixture.
The intended image remains
`sha256:76c55d9151bc43a71cd24a57d334257e872ecd6d9086b652c53099a3c8e50cb0`.

The first bounded read-only inspection verified retained local PostgreSQL
container `2c0a7f5cbcb15f73b583b72b57effa09758cd1dafc62e7968f6dacaca7e75cd9`
is running with role `pg` and owned session `6986e6a7eae94f038dcad20196d153b6`.
The second bounded command read `/proc/meminfo` through that exact container on
the explicit local OrbStack socket. Both commands exited 0, with direct children
reaped and clean process groups under their 15-second watchdog bounds.

At **2026-09-11 16:29:14 UTC**, the VM reported:

| Measure | KiB |
| --- | ---: |
| MemTotal | 20,522,460 |
| MemAvailable | 11,657,756 |
| Existing launcher minimum | 12,582,912 |
| Shortfall | 925,156 |

Available memory was approximately **11.1177 GiB**, below the unchanged 12 GiB
runtime admission requirement in `task9_owned_linux.py`. This current receipt
refreshes the older 11.50 GiB measurement; it does not infer that memory was
exhausted or that Rust tests failed.

No new worktree, image retag, launcher repin, PostgreSQL fixture, runner, or Rust
workload was created by this preparation. Existing containers were not started,
stopped, restarted or deleted. This avoided allocating another fixture for an
already-failing admission condition. The guard was not lowered.

The frozen build remains successful Linux compilation and focused Dockerfile
test evidence. Its database-backed runtime qualification is **not executed**.
Once resource admission can pass, the frozen image must be selected through an
isolated source-matching launcher; the current development branch's runtime
source remains different. Neither this receipt nor the frozen image qualifies
the newer HEAD or production readiness.

Original ownership and memory stdout/stderr logs plus watchdog receipts are
retained beside this document. No production controller or real Proxmox host
was accessed by these two local Docker checks.
