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

The supervisor socket now accepts `publish_start_pe_running_power`, with exact
StartPe identity, canonical request, accepted receipt, daemon generation, explicit
observation timestamp, and Running/unlocked power. It validates the request and
receipt, reloads the immutable StartPe completion, derives its original acceptance
time, and records a later power sample. It refuses worker publication, future or
older-than-five-second samples, duplicate timestamps, wrong generations, and a
new daemon generation after restart. Refusals leave journal bytes unchanged.
Publication never derives Running from a receipt or configuration snapshot.
The reply is the recorded observation with its original supplied timestamp.

This is a publication boundary. A supervisor must still obtain fresh power
evidence through its read path; neither `World` nor the immutable StartPe
publication supplies it. `Scheduler::fixture_stop_authority` independently
reloads PostgreSQL guarded-grace/current-lease facts, but no connected supervisor
consumer joins that result with this command and `AdmitStop` yet. Public authority
fields remain assertions supplied by the supervisor. The command creates no
dispatch release, stop attempt, effect, stop receipt, or Stopped observation.
Stop submission, stopped-power publication, reconciliation and production
acceptance remain open.
