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

## Bounded fixture retry passed

After bounded Docker health and owned-inventory reads succeeded, the same pinned
image passed fixture mode. Evidence is retained in
`linux-fixture-6f582095-owned-retry-1/`; `0025.json` records exit 0 with no supervisor
failure, and `0025.stdout` contains 34 successful test-result groups and no failed
test/panic markers. The attached invocation took approximately 52.94 seconds.
Launcher HEAD was `e235548f64fff0fe981e6ef0160325b45a31bd02`; the source remains the
frozen `6f582095` image, not any later worktree source.

Receipt session is `4790a8a6b596422993ca201029a73fdf`. PostgreSQL container
`5718e33e38e93d6438bd846ba5aa06ca557f3708c325cad6054c3af6bb314329`
and exited-success runner
`441640201d4a6fc1cf823526e9ef1b7b55ebe7ce664bbc18be6ff96925bf505c`
are retained. Final inspection reports OOMKilled false and zero restarts for
both. Admission used cgroup2; PostgreSQL's recorded memory event counters were
all zero, with 102,662,144 bytes current against the 6 GiB cap. The runner was
admitted with the 4 GiB memory/swap cap; its initial cgroup check passed. These
are bounded observations, not periodic peak-memory or post-run runner cgroup
sampling. The launcher still explicitly reports qualification INCOMPLETE.

This establishes the requested feature fixture invocation for the frozen source.
The full database/controller workload, later source and broader production
readiness require their separate evidence. No existing containers or volumes
were deleted or restarted.
