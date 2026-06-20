# Task 1 Report: Provision Naming Helper TDD

## Status

DONE

## Scope

Implemented Task 1 only:

- `autopilot-proxmox/frontend/src/provisionNaming.test.ts`
- `autopilot-proxmox/frontend/src/provisionNaming.ts`

No React imports or page changes were added.

## TDD Evidence

### RED

Command:

```bash
cd /Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/provision-launch-redesign/autopilot-proxmox/frontend
npm test -- src/provisionNaming.test.ts
```

Observed expected failure:

- Vitest failed to resolve `./provisionNaming`
- `src/provisionNaming.ts` did not exist yet
- No production helper code had been written before this failure

### GREEN

Command:

```bash
cd /Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/provision-launch-redesign/autopilot-proxmox/frontend
npm test -- src/provisionNaming.test.ts
```

Observed passing result:

- Test Files: 1 passed
- Tests: 6 passed

## Implementation Notes

- Added `WINDOWS_COMPUTER_NAME_LIMIT = 15`.
- Added `deriveProvisionNaming(runTag)` to keep the full run tag as the group tag while deriving a short hostname pattern.
- Added `previewHostnamePattern(pattern)` using backend-style preview replacements:
  - `{index}` -> `01`
  - `{vmid}` -> `105`
  - `{serial}` -> `SERIAL01`
- Added `normalizeHostnameBase(value, reservedSuffixLength)` to normalize, truncate, and avoid numeric-only hostname bases within the Windows computer-name limit.
- Kept hostname previews short and safe while allowing the group tag to remain long/descriptive.

## Additional Verification

Command:

```bash
cd /Users/Adam.Gell/.config/superpowers/worktrees/ProxmoxVEAutopilot/provision-launch-redesign/autopilot-proxmox/frontend
npm run typecheck
```

Observed passing result:

- `tsc --noEmit` completed successfully.

## Concerns

- `./skill.sh status` and `./skill.sh docs "Provision naming hostname pattern group tag"` both reached the live containers/tunnel but failed MCP requests with HTTP 401 Unauthorized, so MCP docs were unavailable in this session. I used the task brief and checked-out source instead.
