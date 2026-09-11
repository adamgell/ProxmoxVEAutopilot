# Current-source Linux fixture qualification attempt

This attempt was run on 2026-09-11 from the isolated macOS worktree using the
owned launcher at controller revision
`1450a556e1ad037472b49319c5f98a5b23cdec87`. The runner image was pinned to
the exact amd64 image `sha256:7178b72fa5b4d1f8cc1fc1ecf5f6896909f2a3e7eead32733e4a6be450eaaef9`,
whose embedded source seal is `a607861f24cac19cad4565365df2a29b58a33e0b`.

The host memory admission check passed in this attempt: `MemAvailable` was
`12,762,612 kB`, above the launcher's 12 GiB threshold. PostgreSQL was created
under the retained session marker
`7facdf9bcf614125980faac12c230517`, with the exact receipt in `receipt.json`.

Qualification did not proceed because the bounded Docker `create` operation
for the runner exceeded its 60-second child deadline. Receipt `0024.json`
records `exit: -9` and `failure: ValueError: child deadline`. Read-only Docker
inspection found the runner identity retained in `Created` state even though
the launcher did not receive a completed create response; `state.json` remains
`qualification: INCOMPLETE` with `pending: runner`. The PostgreSQL and runner
identities and all numbered receipts are retained for diagnosis. This is an
infrastructure admission failure, not a Linux runtime qualification pass.

This evidence proves neither owned Linux execution nor production readiness.
No production controller, `192.168.2.4`, real Proxmox state, deployment,
cutover, or Ansible retirement was performed.
