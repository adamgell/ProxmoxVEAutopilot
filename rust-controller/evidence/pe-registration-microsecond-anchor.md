# Exact microsecond registration anchor

`PeRegistrationAnchorV2` is an additive versioned representation of original
StartPe operation/event, opening time in integer Unix microseconds, original
registration budget, and checked derived deadline. It retains PostgreSQL's
microsecond precision without floating-point arithmetic or millisecond rounding.

Strict decoding requires an independently reconstructed original anchor and
rejects event/time/budget/version substitution. V1 conversion is explicit:
`from_v1` revalidates the historical anchor and checks multiplication/addition
overflow; `try_into_v1` refuses any non-millisecond-aligned opening or deadline.
Existing v1 wire shape, arming proposal, and database precision guard are unchanged.

Tests pin 1,000,123 microseconds plus 60 seconds to 61,000,123 microseconds,
reject its lossy downgrade, and prove a millisecond-aligned round trip. They also
cover corrupted v1 deadline, overflow, missing/unknown/oversized input, nil
identity, and original-anchor substitution.

Validation: osdeploy-adapter 53 tests plus 6 doc tests passed; strict all-target
fixture-ipc Clippy for osdeploy-adapter, postgres-store, and operation-controller,
workspace fmt, and diff checks passed.

This is representation/conversion only. The PostgreSQL diagnostic probe still
uses v1 and refuses submillisecond anchors until explicitly integrated with v2.
Timestamp extraction must itself be exact; this type does not bless a caller
that previously truncated a higher-precision value. Authenticated witness,
atomic session persistence, dispatch, satisfaction, and production paths remain
disabled and unchanged.
