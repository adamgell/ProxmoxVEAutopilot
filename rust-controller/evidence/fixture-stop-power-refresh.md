# Fixture stop admission: durable power refresh

The original stop-admission frame required StartPe's completion-time power
observation to occur after the current EnsureStopped lease check. That cannot
represent an ordinary elapsed shutdown grace: StartPe completes before grace.

The fixture journal now permits a separate, strictly later Running observation
for the exact accepted StartPe effect. It preserves operation, binding, receipt,
VM, effect sequence and original acceptance time. StartPe task completion remains
immutable, and a second completion publication is refused. Stop admission binds
the latest power observation while retaining the original physical completion
predecessor. Original version-1 frames without the optional current-power field
still recover and replay without changing bytes.

Validation covers stale pre-grace evidence, altered acceptance time,
nonmonotonic observations, duplicate completion, recovery, and admission replay.
The tests assert that refresh and admission add no attempt or effect and never
publish Stopped. All three pve-port power unit tests passed; all-feature,
all-target pve-port Clippy passed with warnings denied. Formatting and whitespace
checks passed.

This is the durable evidence seam only. A supervisor must still obtain fresh
power evidence through its read path. PostgreSQL guarded-grace/current-lease
authority has not yet been connected to the admission request. Public authority
fields remain assertions supplied by the supervisor. Stop release, submission,
stopped-power publication, reconciliation and production acceptance remain open.
