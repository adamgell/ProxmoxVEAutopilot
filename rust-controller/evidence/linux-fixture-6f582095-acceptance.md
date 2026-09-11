# Frozen Linux build and fixture launch attempt

Source `6f5820958602a9c7ef171bae825af5a382eef122` was streamed to Docker as
`git archive`, excluding concurrent worktree/untracked changes. The build used
the existing Dockerfile on local OrbStack, Linux AMD64. All release compilation,
all-feature test compilation and Dockerfile runtime gates completed successfully.
Build output and tree manifest are retained in `linux-fixture-6f582095-build/`.

Resulting image ID:
`sha256:1d3600310cc29002b2eeeeaf3445abe4247e6d9fa2033c0bad489150f58db97a`.
Image inspection reported Linux AMD64 and the frozen source environment value.
A read-only, network-disabled 256 MiB container verified 254 copied source blobs
against their frozen Git object IDs. Its output is retained in
`linux-fixture-6f582095-build/source-verification.txt`; container
`fixture-source-seal-6f582095` was retained. The build log contains the image
manifest/config/export identifiers. Launcher pin commit is `cb5ad2d`.

All 12 launcher unit tests passed. The fixture invocation used:

```sh
python3 rust-controller/scripts/task9_owned_linux.py --runner-image rust-controller-task9:local --mode fixture --evidence rust-controller/evidence/linux-fixture-6f582095-owned
```

It refused before owned container creation: `0003.json` records PostgreSQL image
inspection killed at the command deadline (3-second total bound, including the
reserved 2-second cleanup interval). No state/fixture receipt was created, and
there is no owned-v1 runtime/cgroup/OOM result to claim from this attempt.
Subsequent bounded Docker `_ping` timed out after 5.007 seconds with no bytes;
container inspection also stalled. Only the diagnostic CLI/shell processes were
terminated. No containers or volumes were deleted, and no engine restart or
production operation occurred.

Build runtime evidence therefore exists for this frozen source, including the
feature fixture tests, but the separate receipt-bound fixture qualification
remains pending local Docker responsiveness. Retry with a new evidence directory
only after bounded `_ping` and image inspection succeed. Do not replace or erase
the failed launch records. Full database/service/recovery qualification and
production readiness are not established by this build.
