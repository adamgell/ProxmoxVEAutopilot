# Current-source Linux fixture qualification attempt

This attempt used launcher revision `2144f2afed11585016230215903c1e418010ba34`,
the pinned amd64 image
`sha256:7178b72fa5b4d1f8cc1fc1ecf5f6896909f2a3e7eead32733e4a6be450eaaef9`,
and image source seal `a607861f24cac19cad4565365df2a29b58a33e0b`.

The launcher correctly refused before runner creation because the retained
PostgreSQL-side `/proc/meminfo` sample reported `MemAvailable:
12,318,252 kB`, below the required 12 GiB (`12,582,912 kB`) threshold. The
PostgreSQL identity and numbered receipts are retained under the new session;
no runner was started. `state.json` remains `qualification: INCOMPLETE` with
`pending: pg`.

This is a resource-admission refusal, not Linux runtime qualification. No
production controller, `192.168.2.4`, real Proxmox state, deployment, cutover,
or Ansible retirement was performed.
