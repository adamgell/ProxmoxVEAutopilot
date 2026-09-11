# Exact-source Linux/amd64 build and direct fixture run

This directory records a bounded exact-source Linux/amd64 build for commit
`becaf0e28048c70d8bde9bc70bf279b4ca7cafa5`.

## Build

The build used the pinned `rust-controller/Dockerfile.test` workflow with
`--platform linux/amd64` and `CONTROLLER_GIT_SHA` set to the exact commit.
The resulting image is:

```text
sha256:7c766f2c552751bf988e20cf94a33e3309a17f41f2dbd26c7531cf63a02fd560
```

The Dockerfile's release builds, workspace test precompilation, pve-port,
operation-controller, PostgreSQL-native, artifact-index, fixture-ipc,
osdeploy-adapter, controller-service, and doctest gates completed successfully.
The complete bounded output is in `build.log`.

## Direct Linux/amd64 feature run

The sealed image was run with network disabled and `RUST_MIN_STACK=16777216`:

```text
cargo test --offline --locked --manifest-path rust-controller/Cargo.toml \
  -p pve-port --features fixture-ipc -- --test-threads=1
```

The run passed 36 unit/integration tests, one ordinary doctest, and 22
compile-fail doctests. Its complete output is in `fixture.log`.

This direct run is not the owned-v1 PostgreSQL namespace qualification. The
tracked `task9_owned_linux.py` launcher is intentionally sealed to its older
fixture image/source and therefore was not retargeted or weakened for this
current-source build. No production, Proxmox, retained container, or image was
deleted or mutated.

## Seals

- `image.txt` records the image identity, architecture, OS, and source build
  variable.
- `source-tree.txt` records the exact tracked Rust-controller tree at build
  time.
- `build.log` and `fixture.log` are retained verbatim command output.
