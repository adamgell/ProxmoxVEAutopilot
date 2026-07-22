# Resume: onboarding wizard implementation

Date opened: 2026-05-27
Last touched: 2026-05-27 15:15
Branch: `claude/naughty-buck-700ffc`
Worktree: `/Users/Adam.Gell/repo/ProxmoxVEAutopilot/.claude/worktrees/naughty-buck-700ffc`

## What is done

5 of 14 plan tasks committed. Plan + spec are committed and tracked. Cumulative commit log:

```
57817b6 feat(onboarding): frontend persistence layer with ETag + retry
db0c2c1 fix(onboarding): revert state-machine gate bypasses; thread state in traversal test
15e9a93 fix(onboarding): add non-null assertion for STEP_ORDER index access (strict TS)
ca4fa34 feat(onboarding): pure-reducer state machine with vitest coverage
cc9d210 fix(onboarding): Jinja-safe JSON for onboarding bootstrap attribute
412c142 feat(onboarding): bootstrap injection + ShellIndexPage hero CTA + Settings nav entry
01eca1a feat(onboarding): state CRUD endpoints with stubbed probes
69518da fix(onboarding): address Task 1 code review (set_launched_run errno, merge tests, unused import)
fda4ef7 feat(onboarding): pg state store with ETag concurrency
```

| # | Task | Status |
| - | ---- | ------ |
| 1 | onboarding_pg + ETag CRUD | DONE |
| 2 | onboarding_endpoints router + intake_secrets + probe_lock + already-configured aggregator | DONE |
| 3 | Bootstrap shape + ShellIndexPage hero CTA + Settings nav | DONE |
| 4 | Frontend pure-reducer state machine | DONE |
| 5 | Persistence layer with toWire/fromWire and retry | DONE |
| 6 | OnboardingPage shell + StepRail + WelcomePersonaStep + AlreadyConfiguredCard | TODO |
| 7 | IdentityStep + AD probe (python-ldap) | TODO |
| 8 | TenantStep + tenant probe | TODO |
| 9 | ArtifactStep + probe + build-resume | TODO |
| 10 | ReviewLaunchStep | TODO |
| 11 | Launch transaction (onboarding_launch.py + endpoint) | TODO |
| 12 | Setup-status projection (onboarding_phases.py) | TODO |
| 13 | OnboardingSetupPage with polling | TODO |
| 14 | Live E2E walkthrough + runbook | TODO |

## What to know before resuming

Two real bugs were caught by reviewers during tasks 1-5 and fixed inline. Mentioning them so the next agent does not re-introduce the same patterns:

1. **Jinja auto-escape**: The bootstrap payload is injected into the React shell via a `data-onboarding="..."` attribute on `#react-root`. The render context value is already JSON-encoded; the template must use `{{ onboarding_json | safe }}` to bypass Jinja's auto-escaping, or the frontend will see `&quot;` and silently fall back to `{"status": "absent"}`.

2. **State-machine gate hacks**: Task 4's plan-test does Next/Next/Next without filling fields. The first implementer set `initialState().tenant.skipped = true` and bypassed the artifact gate to make that test pass. Both are wrong: `skipped` must default to `false` and the artifact gate must require selection. The test was rewritten to thread state correctly between advances. Future tasks that touch the state machine should not re-introduce these bypasses.

## How to resume

Use the subagent-driven-development skill from this point. The pattern that worked:

1. For each remaining task in order:
   - Dispatch an implementer subagent with `Agent` (general-purpose). Point it at the exact plan line range rather than pasting the full task text (saves controller context). Pass the working dir and the project hard rules (ASCII hyphens only, TDD).
   - When implementer reports DONE, dispatch a combined spec + quality reviewer pointed at the diff (`git diff <prev-sha>..<task-sha>`).
   - If reviewer flags issues, dispatch a focused fix subagent with the specific issues quoted.
   - Re-verify the fix (a `Bash` check is enough for surgical fixes).
   - Update the task tracker, move to the next task.

Plan line ranges for the remaining tasks (use these with `Read` + `offset/limit`):

| Task | Lines |
| ---- | ----- |
| 6 | 1460-1896 |
| 7 | 1897-2260 |
| 8 | 2261-onward (grep `^## Task 9:` to find end) |
| 9, 10, 11, 12, 13, 14 | similar; grep `^## Task N:` boundaries |

## Resume prompt to paste in a fresh session

When you open a new Claude Code session in this worktree, paste this:

```
Continue executing the onboarding-wizard plan at task 6. Read
docs/superpowers/RESUME-onboarding-wizard.md for the current state,
known-fixed bugs, and the subagent-driven pattern that worked for
tasks 1-5. Then invoke the superpowers:subagent-driven-development
skill and dispatch task 6's implementer with the plan file slice at
lines 1460-1896. Continue continuously through task 14 unless blocked.
```

## Sanity checks before kicking off

Before the new session dispatches anything, run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/.claude/worktrees/naughty-buck-700ffc
git status                                  # should be clean
git log --oneline -1                        # should be 57817b6
git branch --show-current                   # should be claude/naughty-buck-700ffc
cd autopilot-proxmox && pytest tests/test_onboarding_pg.py tests/test_onboarding_endpoints.py tests/test_react_shell.py -q
cd frontend && npx vitest run src/onboarding/ src/routes.test.ts
```

Both test suites must be green before adding more work on top.
