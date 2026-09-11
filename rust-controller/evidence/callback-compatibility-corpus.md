# Callback compatibility corpus and Rust mapping

Status: source-derived corpus and proposed mapping; Rust handlers, session rows,
action exposure and result transactions are **unimplemented**. This document
does not approve a callback design or assert runtime parity. All examples use
synthetic identifiers; `R`, `S`, and `A` denote a run, step and agent. Tokens are
symbolic, never captured credentials. Source paths below are repository-relative.

## Pinned source evidence

| Source | SHA-256 |
| --- | --- |
| `autopilot-proxmox/web/osd_v2_endpoints.py` | `bbf026596a46b64ada7a327e1c6f1f1b7ff465e085027b97ad7b1fdd31a32d81` |
| `autopilot-proxmox/web/ts_engine_pg.py` | `7b841441f7b6ca621d4dbf72d5590e6405c09b298ce6e6f6a40c5e9b3b84097a` |
| `autopilot-proxmox/tests/test_osd_v2_endpoints.py` | `b362e94b21bd9a0f6f81f234ff5cdeff5ec06fab2e205af8fb03ed5471e2d68a` |
| `autopilot-proxmox/tests/test_ts_engine_pg.py` | `3700d94e98125477148f29dc20e06734b3263f178ecf37c696fb294db60bb6c4` |

References below abbreviate these files as endpoints, engine, endpoint-tests and
engine-tests respectively. Tests were inspected, not rerun for this documentation
change. Framework-level validation details such as exact 422 bodies are not
asserted without a pinned framework/runtime fixture.

## Request and response corpus

All paths use `/osd/v2/agent`. All endpoints below are POST. Except registration,
handlers require a bearer and matching run token. `_require_bearer` (endpoints:174)
returns 401 with detail `missing bearer`, `token expired`, or `invalid token`;
`_require_run_token` (:185) returns 403 `token/run mismatch`. `_sign` (:170) uses a
24-hour TTL. A renewed bearer is an opaque response value, not a stable golden
string. Registration itself has no bearer dependency at this handler boundary.

| ID | Input and precondition | Exact source-observed outcome | Source/test |
| --- | --- | --- | --- |
| REG-1 | `/register`, `{"run_id":"R","agent_id":"A","phase":"winpe"}`; run exists without reboot cursor | 200 object with `run_id`, `agent_id`, `phase`, `bearer_token` | endpoints:601; endpoint-tests:646 |
| REG-2 | Same body, missing run | 404 `run not found` | endpoints:605–609; source-only case |
| REG-3 | Existing cursor step is `awaiting_reboot`; register a new full-OS agent | Calls `mark_reboot_complete`; step becomes `done`, event `step_reboot_complete`, run state recalculated; registration response unchanged | endpoints:610–620; engine:2307; endpoint-tests:963 |
| NEXT-1 | `/next`, `{"run_id":"R","agent_id":"A","phase":"winpe"}`; one eligible pending step | 200 `{run_id,phase,actions:[ACTION],reboot_required:false,bearer_token}`; claim increments integer attempt, marks running, records agent/time and `step_claimed` | endpoints:628; engine:1921; endpoint-tests:646 |
| NEXT-2 | No eligible step, terminal run, or blocked predecessor in same phase/`any` | Same response with `actions:[]`; no claim | engine:1948–1980; engine-tests:900,944,1029 |
| NEXT-3 | `batch_size:20` | Clamp to ten; values below one clamp to one; zero uses fallback one. Subsequent claims in same request may bypass running predecessors claimed by that same agent | endpoints:632–644; engine:1952–1965; source-derived batching case |
| NEXT-4 | Explicit capabilities, wrong phase, or default full-OS client kind restriction | Kind filter applies when nonempty; phase must match or `any`; full-OS agent cannot claim WinPE-only step | endpoints:274; engine:1931–1969; endpoint-tests:735; engine-tests:993 |
| RES-1 | `/step/S/result`, `{"run_id":"R","agent_id":"A","phase":"winpe","status":"success","message":"ok"}`; no required reboot | 200 `{ok:true,step:UPDATED,bearer_token}`; step done, run next state, event `step_done`; default data `{}` | endpoints:681–725; engine:2181; endpoint-tests:646 |
| RES-2 | Unknown S / S belongs to another run / phase mismatch (excluding `any`) | Respectively 404 `step not found`, 403 `step/run mismatch`, 409 `step phase mismatch` | endpoints:689–698; endpoint-tests:1024 verifies 409 |
| RES-3 | Unsupported status string | 400 detail `unsupported step status: VALUE` | endpoints:707–708; engine:2263 |
| RES-4 | `success` with required reboot, or `reboot_required` | Step/run `awaiting_reboot`, unfinished timestamp, event `step_awaiting_reboot` | engine:2212–2234; engine-tests:763 |
| RES-5 | `failed`, attempt <= retry_count | Step returns pending, claim fields cleared, warning event; otherwise failed (run may continue if continue_on_error) | engine:2235–2258; engine-tests:844 |
| RES-6 | `failed` while step already awaiting reboot | Append `step_late_failure_ignored`, preserve awaiting-reboot step, return it | engine:2200–2210; engine-tests:798 |
| RES-7 | Repeat success / conflicting terminal result / another agent submits | No general immutable-result or claimed-agent comparison in `complete_step`; normal update/event path executes. This is a source observation, not a desired Rust guarantee or executed replay test | engine:2181–2304 |
| REBOOT-1 | `/rebooting`, `{run_id:R,agent_id:A,phase:winpe,step_id:S}` | Calls complete_step with `reboot_required`, default message `agent is rebooting`; 200 `{ok:true,step,bearer_token}`; ValueError becomes 400 | endpoints:728–743; endpoint-tests:963 |
| PHASE-1 | `/phase-complete`, `{run_id:R,agent_id:A,phase:winpe}` | 200 `{ok:true,run_id,phase,phase_complete,incomplete,bearer_token}`. Incomplete contains `{step_id,kind,state}` for matching phase/any steps in pending/running/awaiting_reboot | endpoints:746–770; source-only case |
| PHASE-2 | Matching steps only failed/done/skipped | They are excluded from incomplete; `phase_complete:true` does not itself prove success or mark a Rust milestone | endpoints:753–761; source-only case |

Registration optional fields: `computer_name:null`, `build_sha:null`,
`capabilities:[]`. Next optional fields: `batch_size:1`, `capabilities:[]`.
Result defaults: `message:null`, `data:{}`. Reboot message defaults null.
Phase-complete requires the three common string fields. Models are at endpoints:31–76.
Do not infer closed unknown-field handling, UUID validation, or attempt binding
from these string-based Python models.

`ACTION` is the exact key set from `_action_from_step` (endpoints:190):

```json
{"step_id":"S","kind":"partition_disk","phase":"winpe","attempt":1,"timeout_seconds":300,"retry_count":0,"retry_delay_seconds":0,"reboot_behavior":"none","params":{},"content":[]}
```

The numeric/reboot values above are illustrative fixture values; actual values
come from the stored step. `params` comes from `resolved_params_json` (or `{}`),
and content from `content_for_step`. Some domain-role kinds resolve credentials
at exposure time (:192–200); the corpus must use synthetic redactions and must
not read credential storage. Tests at endpoint-tests:927 and :944 pin reboot and
retry fields. There is no attempt identifier in the result body.

## Transaction and compatibility distinctions

`claim_next_step` uses ordered `FOR UPDATE SKIP LOCKED`, writes the claim and
event, then `_commit`. `complete_step` updates step/run and appends its event,
then `_commit` before returning. The endpoint subsequently invokes CloudOSD and
OSDeploy synchronization (:709–720). Therefore a shared connection context does
not establish atomicity across those callbacks; inner commits are visible in the
engine. Rust must not copy this as proof of atomic result/successor transition.

`/rebooting` does not perform the result endpoint's explicit phase check; it
passes run/step to the engine. Registration can finish the awaiting-reboot cursor
without comparing the old claiming agent. General duplicate result immutability,
conflict auditing and original-attempt witnesses are not enforced by the inspected
legacy completion path. These divergences require explicit migration decisions,
not silent claims of byte-for-byte semantic compatibility.

## Rust mapping and acceptance gates

All rows below are pending implementation except the pure GuestActionIdentity
primitive. Existing selected requirements come from
`docs/superpowers/specs/2026-09-05-rust-osdeploy-durability/next-durable-transaction-boundaries.md:87–91`
and `next-durable-main-decisions.md:15`.

| Surface | Required Rust identity/transaction mapping | Unimplemented proof |
| --- | --- | --- |
| Registration | Bind actual server session, role, label and credential issuance to immutable run/VM scope; PeRegister needs bootstrap plus subsequent run-bearer witness | Bootstrap alone must not certify VM identity; early registration, wrong role/session, reboot and restart cases |
| Action exposure | Stable step_id equals operation UUID, original logical attempt retained, immutable action, native retry_count=0, no automatic reoffer | Serialize exact timeout/reboot/params/content contract; atomic exposure and response-loss replay; decide integer legacy attempt representation |
| Result | Resolve stable operation plus original exposed attempt/session witness; validate result against immutable action before selecting first admissible terminal result | Duplicate equivalent replay, conflicting payload audit, late failure replay, cross-agent/session rejection, stale attempt and cancellation races |
| Result plus successor | Persist terminal result, selected decision, journal/outbox and successor eligibility coherently in existing family transaction | Server-fixed five-second result-to-immediate-successor continuation; unresolved continuation returns 503; restart does not create another attempt |
| PE-complete/grace | Selected boot-files-staged milestone and conflict handling; atomically create original grace anchor and truthful grace wait activation/attempt/due proof with selected success | Crash between milestone and grace activation; preserve original deadline/contact times; no invented past attempt |
| Reboot / phase-complete | Treat callback as scoped evidence; preserve exact original operation/session; phase-complete is a report, not blanket stage satisfaction | No callback substitutes for host QGA or persistent-agent heartbeat; distinguish failed phase from successful milestone |
| Writer transition | Existing authority generation must also fence legacy job claims/results and establish quiescence | Actual dual-executor transition, rollback and late legacy writes remain separate from API corpus |

The next implementation input is a closed mapping for each corpus ID to accepted
Rust request, response, error, identity witness and atomic transaction. The
referenced `callback-main-decisions.md` and `task-2-brief.md` are still absent from
the reviewed checkout/inventory per `callback-contract-gap.md`; this corpus does
not reconstruct their missing decisions. Resolve especially session/token claims,
legacy attempt numbering, replay/conflict HTTP responses, integer timeout
serialization and reboot semantics before enabling callbacks. Keep StartPe and
later callback-dependent stages gated until real session preparation and atomic
arming exist. No live requests, production changes, or Rust routes were added.
