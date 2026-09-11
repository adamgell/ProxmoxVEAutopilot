# StartPe closed capture through the controller boundary

The code seam identified in `startpe-controller-send-integration-gap.md` is now
implemented. The owned-child StartPe runtime/crash proof remains open.

- `ControllerFixturePort::original_start_pe_response` exposes only the existing
  closed response value. Non-StartPe ports default to no fixture response.
- A bound StartPe fixture adapter rejects absent original capture, including a
  restored adapter. It cannot silently choose semantic-only persistence.
- Controller `submit_and_capture_once` now uses the actual controller-port type.
  After its one successful consuming send, it retrieves the closed capture once,
  binds it to original dispatch, and verifies its semantic receipt equals the
  returned receipt. Each existing bounded persistence retry selects the atomic
  fixture writer when that input is present. Defaults/non-StartPe continue through
  the existing generic writer. No resend or new authority is introduced.

## Verification and limits

The new trait-object capture assertion first failed compilation because the
accessor was absent. After implementation:

- Genuine IPC/PostgreSQL composition test
  `fresh_controller_clone_then_disk_capacity_then_configure_pe_reaches_satisfied`:
  1/1 passed in 14.52s. This verifies the trait-object boundary preserves exact
  original receipt/provenance alongside existing rollback/replay/reload proofs.
- `pve-port --features fixture-ipc --test fixture_post_dispatch`
  `synchronous_configure_publication_requires_resize_and_invalidates_on_restart`:
  1/1 passed in 1.06s, including new missing/restored-capture rejection assertions.
- All-feature/all-target Clippy for operation-controller and pve-port with warnings
  denied passed. Default-feature operation-controller library compile, formatting,
  and diff whitespace checks passed.

An initial combined test run failed its existing pre-StartPe prefix deadline in
5.44s while other compilation was active. The isolated rerun above passed without
changing deadlines; contention is a suspected cause, not a proven diagnosis.

The combined fixture test still manually orchestrates StartPe after running the
controller prefix. It does not yet exercise this new StartPe send branch through
an owned `OsDeployController` subprocess. That integration and SIGKILL-at-write
barrier remain the next proof; this checkpoint does not claim them complete.

No production mutation, backfill, replacement-generation authority, or physical
stop was added.
