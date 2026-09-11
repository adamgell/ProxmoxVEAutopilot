# Current-source Linux qualification status

This is a point-in-time source-to-evidence audit recorded on 2026-09-11. It
does not claim a Linux runtime pass.

## Source identity

The worktree revision audited was:

```text
a5f3fc503fa8cb6f333ea4ad6cdacc900e088c37
```

The latest retained exact-source Linux/amd64 runner image was built from:

```text
f92a62872b554a664df96e05d041876e7371cb8d
sha256:5f5ddae5dd93381cc02d944d8ae397bb0b1a085f65623499122fb0f798b1caa6
```

The two revisions are not interchangeable. A read-only comparison of the
`rust-controller` runtime tree reports material differences, including changes
to controller, PostgreSQL store, fixture IPC, migrations, scheduler, and
owned-Linux launcher code. Therefore
the retained `f92a6287` image cannot be relabeled or reused as evidence for
`a5f3fc50`; changing only `CONTROLLER_GIT_SHA` would be an invalid source seal.

## Gate status

| Gate | Evidence | Status |
| --- | --- | --- |
| Exact archive/image build | `linux-fixture-f92a6287-build-1/` | Proven for `f92a6287` only |
| Current-source image build | No retained image for `a5f3fc50` | Open |
| Current-source owned Linux runtime | Cannot run before the current-source image is resealed | Open |
| Linux production-candidate qualification | Depends on the preceding gates and broader compatibility/recovery gates | Open |

The required next step is a fresh build from an exact Git archive of the
intended current revision, with the matching `CONTROLLER_GIT_SHA`, immutable
image inspection, and source-tree verification before any owned fixture is
created. The launcher must then be pinned to that image and source pair in a
separate committed change. Existing failed and successful historical evidence
must remain preserved rather than rewritten.

All checks in this record are local and read-only. No production controller,
`192.168.2.4`, or real Proxmox state was changed.
