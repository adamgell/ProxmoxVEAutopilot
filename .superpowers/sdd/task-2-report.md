# Task 2 Report: React Provision Layout And Run Tag Fill-Down

## Status

DONE_WITH_CONCERNS

## Summary

Implemented the React Provision launch redesign in the task-owned files only:

- `autopilot-proxmox/frontend/src/pages/ProvisionPage.tsx`
- `autopilot-proxmox/frontend/src/ProvisionPage.test.tsx`

The page now uses a run-tag composer, radio-style boot path rail, launch essentials section, Autopilot enrollment section, CloudOSD desktop section, advanced CloudOSD options, OSDeploy server section, Ubuntu section, and launch review rail while preserving the existing `/api/jobs/provision` form contract and field names.

## TDD Evidence

RED:

- Command: `npm test -- src/ProvisionPage.test.tsx`
- Result: failed as expected before implementation.
- Expected missing behavior was observed: no `Run tag` textbox, no radio-style boot path, no preview/counter, no `Autopilot Enrollment` panel, and no advanced OSDCloud artifact readiness section.

GREEN:

- Command: `npm test -- src/ProvisionPage.test.tsx`
- Result: `1 passed`, `3 tests passed`.

Final requested test run:

- Command: `npm test -- src/provisionNaming.test.ts src/ProvisionPage.test.tsx`
- Result: `2 passed`, `9 tests passed`.

Additional verification:

- Command: `npm run typecheck`
- Result: passed with `tsc --noEmit`.
- Command: `npx eslint src/pages/ProvisionPage.tsx src/ProvisionPage.test.tsx`
- Result: passed with no output.
- Command: `git diff --check`
- Result: passed with no output.

## Implementation Notes

- Added controlled `value`, `onChange`, `readOnly`, and `maxLength` support to local `TextField` and `NumberField` helpers while preserving existing `defaultValue` behavior.
- Added one-time default initialization during `/api/provision/page` load, guarded by `defaultsApplied` so polling does not overwrite operator edits.
- Added `Run tag` fill-down behavior:
  - Updates `Group tag`.
  - Derives `Hostname pattern` until the operator manually edits the hostname field.
  - Keeps the generated hostname preview within the 15-character Windows/Intune/Entra computer-name limit through `deriveProvisionNaming`.
  - Includes `Reset hostname from run tag` using `RefreshCw`.
- Kept `Run tag` UI-only; it does not submit as its own form field.
- Kept submitted boot mode through hidden `boot_mode`; the radio rail does not add an extra submitted boot-mode field.
- Removed visible OSDCloud/OSDeploy artifact combobox ownership from the launch form; readiness is displayed instead.
- Preserved named form controls verified by tests:
  - `profile`
  - `count`
  - `hostname_pattern`
  - `group_tag`
  - `cores`
  - `memory_mb`
  - `disk_size_gb`
  - CloudOSD controls such as `network_bridge` and `os_version`
  - OSDeploy controls such as `osdeploy_network_bridge` and `osdeploy_os_version`
  - Ubuntu controls such as `ubuntu_v2_sequence_id` and `ubuntu_template_vmid`
- Added semantic class names only; did not edit `styles.css`.
- Did not edit backend files.

## MCP Docs Note

Ran `./skill.sh status` at session start. Containers and MCP service were up, but the docs tool probe failed with `401 Unauthorized`, so I used local repository files and the task brief as the fallback source as instructed by `AGENTS.md`.

## Concerns

- Full-project `npm run lint` still fails because of pre-existing unrelated lint errors in files outside this task, including `NetworksPage.test.tsx`, `SdnInlineForm.tsx`, `AgentDownloadPage.tsx`, `NetworksPage.tsx`, and `VmsPage.tsx`.
- The task-owned files pass scoped ESLint.
