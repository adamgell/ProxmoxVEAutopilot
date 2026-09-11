# Fixture IPC Linux requalification recipe

The local OrbStack runner inspected during this audit was Linux AMD64 image
`sha256:d7e20369af0d3148c53dcbc210448f07e9b81d119fc329d99fedbcc24a5c09c4`,
with `CONTROLLER_GIT_SHA=8a98d7f6dc4a73f0c7b741b08d6b27978c56efde`.
The owned launcher pins match it. Later source, including the StartPe durable
publication at `6b958100`, requires a rebuilt image and fresh evidence. An image
source environment variable alone is not a source-content seal.

The full workload now excludes five ignored subprocess entrypoints from direct
execution. Their parent tests still launch those children with owned input.
Intentional ignored owned-storage tests remain selected. The separate fixture
mode executes pve-port with `fixture-ipc` and default ignored-test handling.

From the isolated repository root, after all intended inputs are tracked and
the source is frozen, capture its commit and build using the approved local
engine. Preserve the build output and a manifest of the exact build-context
inputs, including source hashes. Do not substitute production Docker or SSH.

```sh
git status --short
git rev-parse HEAD
docker --host unix:///Users/Adam.Gell/.orbstack/run/docker.sock build --platform linux/amd64 --build-arg CONTROLLER_GIT_SHA="$(git rev-parse HEAD)" -f rust-controller/Dockerfile.test -t rust-controller-task9:local .
docker --host unix:///Users/Adam.Gell/.orbstack/run/docker.sock image inspect rust-controller-task9:local --format '{{.Id}} {{.Os}} {{.Architecture}} {{json .Config.Env}}'
```

After verifying source content, image digest and architecture, update only the
launcher's `RUNNER_IMAGE` and `IMAGE_SOURCE` constants to the verified values and
commit that launcher change. Preserve the distinct image source and launcher
commit. Run the launcher unit tests, then invoke both gates with new, previously
nonexistent evidence directories:

```sh
python3 -B -m unittest discover -s rust-controller/scripts -p test_task9_owned_linux.py -v
python3 rust-controller/scripts/task9_owned_linux.py --runner-image rust-controller-task9:local --mode fixture --evidence rust-controller/evidence/requalification-fixture-current
python3 rust-controller/scripts/task9_owned_linux.py --runner-image rust-controller-task9:local --mode full --evidence rust-controller/evidence/requalification-full-current
```

The launcher preserves its fail-closed image/source, ownership, disk and resource
checks. Its existing 16 MiB test stack setting reaches child processes. Each
workload remains bounded to 1,800 seconds. Verify actual exit records, tests,
receipt/source linkage, retained resource records and qualification limits;
neither a successful build nor either invocation alone proves production
readiness. No build, container workload or production action was performed by
this audit.
