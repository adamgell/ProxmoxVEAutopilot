# Current-source Linux qualification status

This is a point-in-time source-to-evidence audit recorded on 2026-09-11. It
does not claim a Linux runtime pass.

## Source identity

The worktree revision audited was:

```text
1450a556e1ad037472b49319c5f98a5b23cdec87
```

The latest retained exact-source Linux/amd64 runner image was built from:

```text
a607861f24cac19cad4565365df2a29b58a33e0b
sha256:7178b72fa5b4d1f8cc1fc1ecf5f6896909f2a3e7eead32733e4a6be450eaaef9
```

The image source and embedded `CONTROLLER_GIT_SHA` match the exact archive
used for its build. The launcher is pinned to this image/source pair. The
older `f92a6287` image remains historical and is not interchangeable with the
current source; it is neither relabeled nor reused.

## Gate status

| Gate | Evidence | Status |
| --- | --- | --- |
| Exact archive/image build | `linux-fixture-a607861f-build/` | Proven; image/source seal matches |
| Current-source image build | `sha256:7178...eaaef9` with embedded `a607861f` | Proven |
| Current-source owned Linux runtime | `requalification-1450a556-fixture-1/` passed memory admission but runner create hit the bounded child deadline | Open |
| Linux production-candidate qualification | Depends on the preceding gates and broader compatibility/recovery gates | Open |

The next safe step is diagnosis or an explicitly bounded retry of the runner
creation path with a longer child deadline, using a new evidence directory and
the same immutable image/source pair. Existing failed and successful
historical evidence must remain preserved rather than rewritten.

All checks in this record are local and read-only. No production controller,
`192.168.2.4`, or real Proxmox state was changed.
