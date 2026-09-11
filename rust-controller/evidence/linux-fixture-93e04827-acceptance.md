# Frozen 93e04827 Linux fixture proof

Frozen source `93e048279b2dfca67d48952af63f253809190ff8` was supplied through
`git archive` to the existing Dockerfile. Both release builds, all-feature test
compilation and every Dockerfile runtime gate passed. The original build stayed
live through a temporary Docker responsiveness lapse and resumed without restart.
Build logs and source-tree manifest are retained in `linux-fixture-93e04827-build/`.

Image ID is
`sha256:349f1808dcc6b1582f8d633dca4849061c2b5ba4872406acf93947edd5d62317`,
Linux AMD64 with the exact source environment value. A retained read-only,
network-disabled verifier container `fixture-source-seal-93e04827` checked 264
copied source blobs against the frozen Git object IDs. Launcher pin commit
`7808a609a9d8fbbe3fb5a440d62071e1126f1109` passed all 12 launcher unit tests.

The bounded fixture invocation passed in approximately 53.66 seconds, exit 0,
no supervisor failure. `linux-fixture-93e04827-owned/0025.stdout` contains 34
successful test-result groups and no failed-test/panic markers. The recipe was:

```sh
python3 rust-controller/scripts/task9_owned_linux.py --runner-image rust-controller-task9:local --mode fixture --evidence rust-controller/evidence/linux-fixture-93e04827-owned
```

Receipt session: `389fb8b27b014551acd6b784e190fbd4`. Retained PostgreSQL ID:
`52bdd2fd43f2091aaadf4aa6951d8046b89a3116cfcc1214d9d940d9d1282743`.
Retained successful exited runner ID:
`348789a2877ac43e5e952b6547fdfecda8eac72925dab33eb7e02e2ba22ff4e4`.
Both final inspections report OOMKilled false and zero restarts. Admission used
cgroup2 and passed runner's initial 4 GiB memory check. PostgreSQL's recorded
memory current was 100,454,400 bytes under its 6 GiB cap; every recorded memory
event counter was zero. These are bounded observations, not a peak/periodic
resource sampling claim.

The launcher explicitly retains qualification INCOMPLETE. This proves the frozen
source's pve-port fixture workload, not the full database/controller workload,
later source, full session lifecycle or production readiness. Existing and new
containers/artifacts were preserved; no deletion, engine restart or production
mutation occurred.
