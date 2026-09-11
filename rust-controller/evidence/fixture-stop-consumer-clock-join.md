# Fixture stop consumer clock join

The explicit supervisor power source does not yet complete the PostgreSQL to
durable fixture stop-admission path. `Scheduler::fixture_stop_authority` checks
the current committed stop lease using database time. Durable admission requires
the independent Running observation to be at or after that check. Reading the
installed test sample preserves its original observation time.

The regression in `explicit_test_power_refreshes_completed_start_and_fences_restart`
now consumes a valid Running sample, constructs a later lease-check boundary,
and repeats consumption and admission attempts. Admission refuses both attempts;
the durable log remains byte-identical, attempt/effect totals remain two, and
the latest power remains Running. Repeated consumption may itself refuse an
already published sample; neither outcome creates a newer observation.

A connected supervisor needs an ordered protocol:

1. Obtain database-derived authority and the exact committed stop request under
   the current grant, retaining the original lease and stage deadlines.
2. Capture a new independent sample after that database check, bound to the
   accepted physical StartPe identity and the current daemon generation.
3. Admit the exact request before all retained deadlines expire, rejecting
   cancellation, ownership changes, inconsistent history, and stale generations.

The current immutable test sample is keyed by StartPe identity and only permits
byte-identical reinstall. A later sample for the same identity therefore needs
an explicit versioned sampling protocol; rereading or restamping the earlier
sample cannot substitute for it. The public authority DTO alone is also not an
opaque database capability. The PostgreSQL and physical fixture histories must
be joined in one integration proof before a positive consumer is claimed.

Stop dispatch, task receipt persistence, independently observed stopped power,
reconciliation, and accepted-stop worker recovery remain open. A successful
admission is bookkeeping only and cannot establish any of those outcomes.
