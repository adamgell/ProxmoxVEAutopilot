# Main decisions for the next durable phase

Main read the full transaction-boundary note and its complete amendment, then verified current controller-domain transition/state rules. These decisions refine the approved replacement design. They do not authorize implementation while registration is still in progress or claim any unbuilt gate passed.

## Selected transition semantics

- Preserve the existing domain and native/generic behaviors. New exceptional transitions are family-private, consume proofs reconstructed under the existing authority/run/operation locking discipline, and use existing atomic journal/outbox/projection semantics. No public target-state input or broad terminal-state repair API.
- A genuinely unactivated inherited scope that expires produces a null-attempt decision and Pending-to-Unknown transition without manufacturing an attempt, lease, historical start time or replacement budget. Its actual immutable scope and anchor remain mandatory.
- Safe reclaim and proven read-only repark preserve one logical attempt, original times and deadline. Missing activity proof, prior dispatch/exposure, cancellation or an expired scope excludes the safe-reclaim rules. Lease tokens may change; operation/attempt identity may not.
- Cancellation records the run fence and all affected stage outcomes atomically: Blocked for proven unexposed work, Unknown for possible exposure, preserving existing terminal decisions. Already elapsed applicable scopes take precedence. Original response capture remains observational, not continuation authority.
- Residual-lease cleanup requires exact linkage to validated terminal history and a recorded revocation decision. Mismatched or unexplained rows fail closed; cleanup does not prove the old sender stopped.

## Grace recovery must survive its activation boundary

The proposed null-attempt expiry rule must not silently turn a crash between selected PE-complete success and grace activation into permanent loss of the approved grace-stop recovery. The eventual PE-complete adjudication transaction must atomically establish the original grace anchor and a truthful durable grace-wait activation/attempt/due proof, or use another independently reviewed mechanism with equivalent recovery guarantees. It must not invent a past attempt after restart or reset the grace deadline. Define exact events and private state transitions in that phase's implementation brief before enabling the callback.

A genuinely unactivated expiry with `InheritedScopeDeadlineExpired` never impersonates the exact selected grace timeout that authorizes conditional stop. Force-stop still requires the original PE-complete/grace evidence, pinned policy, fresh owned running-PE observation and current authority/CAS/lease/mutation deadline. Grace remains Unknown.

## Send and handoff truth

A current database generation check is necessary but cannot atomically fence an external Proxmox request. Select a cooperative service send-admission gate, closed on startup/shutdown/handoff, with joined/drained send-capable tasks before declaring local quiescence. A drain timeout fails the handoff gate. Never recover a consumed or uncertain one-shot permit, and never equate expired leases with stopped writers.

Production replacement still requires demonstrated quiescence/disablement of every prior mutation-capable process, including Python, plus reconciliation of ambiguous requests/tasks, unless an actual independently verified transport-side fence provides that guarantee. This operational gate does not authorize changes to production now. Claim locally gated send initiation and strict durable outcome deadlines, not remote acceptance-time or cross-system exactly-once guarantees.

## Scope and remaining decisions

The initial store/controller implementation may validate its first three fake-PVE stages while callback-dependent stages remain inaccessible. That is an intermediate integration boundary only: the active goal still requires the full sixteen-stage service-driven workflow, compatible guest interactions, restart/fault/recovery proofs and readiness assessment. No substitute fixture predecessors or public bypasses.

Before dispatching the next source phase, pin the complete schema and closed decision payloads, wait versus Unknown-reconciliation projection/query behavior, polling bounds, all lock-order compatibility, and exact callback arming/timeout contracts for stages being enabled. The current registration implementer remains sole source owner.
