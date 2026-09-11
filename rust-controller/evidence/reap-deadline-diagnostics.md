# Deadline-exhausted child reap diagnostics

The c3e38135 Linux gate failed the synchronous-cleanup supervision test after
`native_fake_child_reap_unconfirmed`. The original message did not identify the
fault child versus cleanup child, kill failure, wait failure, or deadline overrun.
The guard can reach its absolute deadline without confirming wait/reap; the
existing zombie-absence assertion correctly rejects that outcome.

Diagnostics now include PID, cleanup elapsed microseconds, original deadline
remaining at cleanup start, deadline overrun, and kill/wait error kind and OS
code. No arbitrary command arguments, output or error text are logged. A missing
wait error means pending, not successful reaping. The last wait error is retained.

The polling loop is extracted without changing its deadline semantics. An
expired deadline allows exactly one nonblocking wait attempt: already reaped is
success; pending or OS error is failure, with no sleep or budget reset. The new
deterministic regression injects pending/error/reaped outcomes without creating
an intentionally abandoned live child. Existing process tests retain real child
kill/reap and zombie-absence assertions.

Validation on macOS: all 14 process tests in operation-controller postgres_native
pass, including the previously failing synchronous-cleanup test; strict all-target
operation-controller fixture-ipc Clippy, formatting and diff checks pass.

This is diagnostic coverage, NOT a Linux failure fix. No cleanup bound was widened
and no zombie accepted. Linux gate remains red until rerun and diagnosis; an
unconfirmed child still follows the existing failure behavior. Production and
real infrastructure are untouched.
