# Current runtime source admission receipt

On 2026-09-11 the actual owned Linux launcher was invoked in bounded `start-pe`
mode against its existing pinned image:

```text
python3 rust-controller/scripts/task9_owned_linux.py \
  --runner-image rust-controller-task9:local \
  --evidence rust-controller/evidence/requalification-2c84e0c4-source-admission \
  --mode start-pe
```

The invocation exited 1 with `owned launch refused: child failed; retained
receipt`. Receipt `0001` records HEAD
`c78c9e9d03a61a36aed1d9ee7bf45d5ba88d8fb8`: a concurrent documentation commit
advanced the requested `2c84e0c4ae311278d221b17199442638cbf9aeec` head. A Git diff
between those two commits over the exact runtime paths returned exit 0, so their
runtime source is identical.

Receipt `0002` records the 3-second bounded Git source comparison against the
image source `518878d7a4c10ddccd2d21656caa7fa3a1308aeb`. It completed normally
with exit 1 and empty output streams. This is a source mismatch, not an
observation timeout or Rust test failure. The runtime path set has 55 changed
files, 8,150 additions and 114 deletions between that image source and
`2c84e0c4`, including newer fixture origin, credential/session and callback work.

The launcher stopped before its first Docker inspection, container creation,
resource observation or workload. Therefore this run creates no fixture and
does not refresh the historical VM-memory measurement. Existing resources were
not inspected, started, stopped or deleted. The existing image pin remains
`sha256:55564eb90ccd5f3773a8dbaf91b2c2e7821770972e376e21a077a60db10e668e`;
its historical build success cannot qualify these changed sources.

The next Linux prerequisite is a frozen archive build from the intended current
runtime source, followed by verified image/source resealing. Only after that
can the existing resource admission and runtime qualification execute. Lowering
the memory guard or merely changing `IMAGE_SOURCE` would not close this source
gap. Current-source Linux runtime qualification remains open.

The two JSON receipts and four stdout/stderr files are original launcher output.
Their stored SHA-256 values match the retained stream bytes. No completed CI
result, Linux workload pass, or production-readiness conclusion is claimed.
