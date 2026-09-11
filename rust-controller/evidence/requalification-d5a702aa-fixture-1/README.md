# Current-source Linux fixture qualification attempt

This attempt used launcher revision `d5a702aa1dbcc4251a620b01afe94a4c569b73b4`,
the pinned amd64 image
`sha256:7178b72fa5b4d1f8cc1fc1ecf5f6896909f2a3e7eead32733e4a6be450eaaef9`,
and image source seal `a607861f24cac19cad4565365df2a29b58a33e0b`.

The owned launcher completed PostgreSQL admission, runner creation, runner
execution, and final inspection successfully. Runner creation used the
separately bounded 300-second admission window. The runner exited 0 without
OOM or restart, and its captured fixture workload reported 364 passed tests
across the Rust test targets plus passing doctests (with the target's stated
ignored/filtered cases retained in the raw output `0027.stdout`).

The launcher intentionally records `qualification: INCOMPLETE`: this lane's
inside protocol reports successful invocation, but it is not the complete
production qualification contract. The retained `state.json`, receipt, and
numbered command records are authoritative. This proves exact-source Linux
fixture execution and bounded test success; it does not prove authentic
production callback transport, physical stop execution, or production
readiness. No production controller, `192.168.2.4`, real Proxmox state,
deployment, cutover, or Ansible retirement was performed.
