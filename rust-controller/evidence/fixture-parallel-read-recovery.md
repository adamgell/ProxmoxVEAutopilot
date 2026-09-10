# Fixture parallel read reliability

The feature daemon suite reproduced transport failures in parallel mode within
three runs: `UnexpectedEof` on Clone reads and `BrokenPipe` on inventory reads.
Temporary daemon diagnostics captured immediate `WouldBlock` from frame reads.
Each test owns a unique UUID directory and original child handle; the failure
was accepted socket mode, not shared endpoint or process ownership.

The macOS accepted stream inherited the listener's nonblocking mode. The daemon
now explicitly switches each accepted stream to blocking mode before using its
existing bounded read/write deadlines. Protocol validation, frame limits, and
durable write ordering are unchanged. Temporary diagnostics were removed.

Verification on macOS: 30 consecutive feature daemon suites with 16 test threads
passed after the mode fix (9 passed, 1 subprocess entry ignored per run). Added
`delayed_payload_is_read_within_the_frame_deadline` exercises a valid request
whose payload follows its header by 20 ms. The resulting feature suite passed
serially and with 16 threads (10 passed, 1 ignored); default parallel suite passed
(8 passed, 1 ignored). Formatting and strict feature daemon Clippy passed.

Existing oversized/partial-frame tests still pass, including the bounded stalled
request case. These checks qualify this macOS framing fix; latest-source Linux
qualification and controller/process takeover proofs remain separate open gates.
