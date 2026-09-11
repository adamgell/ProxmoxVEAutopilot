# Owned-v1 full qualification: launcher `a0e36390`

This retained run used the exact-source runner image sealed to `f92a6287` and
the corrected owned-v1 workload selection from launcher commit `a0e36390`.

- Runner container: `92ebde2b1be31b2b61edabd321f96edbdd3edd79ae60215a7473ba355285f9bb`
- Runner exit: `1`
- OOM/restart: not observed
- DSN-only tests: explicitly excluded because they require
  `PVA_START_PE_SCHEMA_TEST_DSN`
- Native cleanup/reap helper tests: passed in the retained output
- Remaining failure: operation-controller fixture recovery group reported
  `Storage`, `Elapsed`, and `BrokenPipe` outcomes
- Qualification: incomplete; no production or real Proxmox state was changed

`runner-docker.log` is the complete recovered container output. The host
launcher had detached before its final state writer ran, so the Docker exit
state and recovered output are authoritative for this attempt; this directory
is not a successful qualification receipt.
