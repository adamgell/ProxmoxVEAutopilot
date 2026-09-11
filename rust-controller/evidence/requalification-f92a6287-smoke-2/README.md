# Owned-v1 smoke Linux qualification

The current sealed runner image (`sha256:5f5ddae5dd93381cc02d944d8ae397bb0b1a085f65623499122fb0f798b1caa6`, source `f92a6287`) passed host admission and the exact bounded smoke workload. The runner exited `0`, with no OOM/restart condition; PostgreSQL and the runner receipt/state remain retained for audit. The launcher intentionally records `qualification: INCOMPLETE`, so this is smoke evidence only and not full production qualification.
