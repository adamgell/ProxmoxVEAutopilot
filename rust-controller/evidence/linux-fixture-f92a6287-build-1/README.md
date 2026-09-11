# Exact-source Linux/amd64 runner image build

This directory records the approved `rust-controller/Dockerfile.test` build
from exact source commit `f92a62872b554a664df96e05d041876e7371cb8d`.

The build used `--platform linux/amd64` and
`CONTROLLER_GIT_SHA=f92a62872b554a664df96e05d041876e7371cb8d`. All twelve
Dockerfile build stages completed, including release builds, workspace test
precompilation, pve-port, operation-controller, PostgreSQL-native,
artifact-index, fixture-ipc, osdeploy-adapter, controller-service, and
doctest gates.

The resulting image is:

```text
sha256:5f5ddae5dd93381cc02d944d8ae397bb0b1a085f65623499122fb0f798b1caa6
```

`image.txt` records the immutable image ID, architecture, OS, and source
environment variable. `source-tree.txt` records the exact tracked
Rust-controller tree used as the build-context seal. `build.log` contains the
complete Docker output. The launcher was not edited or retargeted during this
build, and no PostgreSQL/runner containers were created by this build step.
