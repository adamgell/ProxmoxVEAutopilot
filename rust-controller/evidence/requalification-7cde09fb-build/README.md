# Linux amd64 build at 7cde09fb

On 2026-09-11, the approved local OrbStack engine built source
`7cde09fbc4f3fb608d9f0ede761cabb347b5c472` using the repository
`Dockerfile.test`, `--platform linux/amd64`, and the matching
`CONTROLLER_GIT_SHA` argument. The tracked tree was clean when Docker
transferred its context, and the runtime paths contained no untracked files.
The retained Git tree manifest identifies the runtime inputs and test playbook.
After the build, the runtime paths still matched that commit exactly.

The bounded build completed in **885.14 seconds**, exit **0**, with its direct
child reaped and process group clean. It passed release compilation, workspace
all-feature test compilation, and all focused test layers in `Dockerfile.test`:
pve-port, controller decision/native-support, artifact-index, fixture IPC,
OSDeploy adapter, and selected controller service tests. The Dockerfile's
filters and ignored tests remain limits on this evidence; this is not the
database-backed full qualification lane.

The inspected Linux amd64 image is
`sha256:02b2be47b0f6dc726ff6e32e1beb2c25d59c2438a5572ba72ab56e699363a0ff`.
Its `CONTROLLER_GIT_SHA` matches the source commit. The build log SHA-256 is
`d659ddf8445e42cb43780ebfc0f1bfbf3b0249577a5aa9444ab0ffb19f405c20`;
the source manifest SHA-256 is
`7fb92b9c57c17289661e9ff643d62d22bbd865cbad5abd0cdaf05d73c3487e84`.

After build completion, a bounded read of `/proc/meminfo` through the already
retained local fixture `2c0a7f5cbcb1` reported
`MemAvailable: 11868952 kB` (approximately **11.32 GiB**), below the owned
launcher's unchanged **12 GiB** admission threshold. Consequently, no new
PostgreSQL fixture or runner was allocated and no database-backed Linux test
lane was invoked. This is a pre-launch resource refusal, not a failed Rust
test or a launcher invocation result. Existing containers remain retained.

The launcher remains pinned to its historical source/image. Before executing
the current-source fixture and full lanes, recheck available memory, verify
that runtime inputs still match this image, update the immutable launcher
pins, and run its unit tests. The 14 existing launcher unit tests passed in
2.06 seconds during this audit, before any pin change.

Raw build, image inspection, source manifest, and memory observations with
bounded-process receipts are retained alongside this file. Production and
real Proxmox were not modified. Full Linux runtime qualification remains open.
