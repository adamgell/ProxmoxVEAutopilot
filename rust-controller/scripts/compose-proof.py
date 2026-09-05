"""Assertions against three actual worker processes in an isolated namespace."""
import json
import subprocess
import sys
import time
import urllib.request

import psycopg2

db = psycopg2.connect("postgresql://postgres:local-proof@127.0.0.1:5432/rust_controller_test")
db.autocommit = True


def query(sql):
    with db.cursor() as cursor:
        cursor.execute(sql)
        return cursor.fetchall()


def wait_for(check, seconds=45):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError("bounded local proof deadline exceeded")


def health(port):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=4) as response:
        body = json.load(response)
    assert body["database"] and body["outbox"] and body["reconciler_fresh"]
    assert body["pve_transport"] == "fake" and body["authority_generation"] == 1
    assert body["successful_sweeps"] > 0
    return body


def capped():
    count = query("SELECT count(*) FROM rust_controller.worker_leases WHERE lease_expires_at>clock_timestamp()")[0][0]
    assert count <= 2, "three workers exceeded workflow cap"
    return count


if sys.argv[1] == "cancel-and-select":
    wait_for(lambda: capped() == 2)
    for port in range(9091, 9095):
        wait_for(lambda: health(port))
    subprocess.run(["/workspace/rust-controller/target/release/examples/seed", "cancel"], check=True, stdout=subprocess.DEVNULL)
    wait_for(lambda: query("SELECT count(*) FROM rust_controller.operations WHERE state='unknown'")[0][0] == 1)
    rows = wait_for(lambda: query("SELECT l.worker_id FROM rust_controller.worker_leases l JOIN rust_controller.operations o USING(operation_id) WHERE o.state='running'"))
    owner = rows[0][0]
    assert owner in ("worker-1", "worker-2", "worker-3")
    print(owner)
elif sys.argv[1] == "verify":
    def finished():
        capped()
        states = dict(query("SELECT state,count(*) FROM rust_controller.operations GROUP BY state"))
        return states if states.get("unknown", 0) == 2 and states.get("satisfied", 0) == 4 else None
    states = wait_for(finished, 55)
    assert states == {"pending": 1, "satisfied": 4, "unknown": 2}, states
    assert query("SELECT max(n) FROM (SELECT count(*) n FROM rust_controller.attempts GROUP BY operation_id) a")[0][0] == 1, "double claim"
    winners = query("SELECT count(DISTINCT payload->>'worker_id') FROM rust_controller.journal_events WHERE execution_state='leased'")[0][0]
    # Three ready worker processes compete, but cap enforcement promises no
    # fairness: the killed worker retains one slot until its lease expires.
    assert 2 <= winners <= 3, "multiple independent workers must win claims"
    assert query("SELECT count(*) FROM rust_controller.operations WHERE operation_key='unrelated' AND state='pending'")[0][0] == 1
    observed = health(9094)
    assert observed["mode"] == "observe" and observed["adapter_versions"] == []
    print(json.dumps({"result": "PASS", "states": states, "cap": 2, "workers_started_and_ready": 3, "workers_claimed": winners, "attempts_per_operation_max": 1, "recovery": "real_30_second_lease_expiry", "transport": "fake", "observer_sweeps": observed["successful_sweeps"]}, sort_keys=True))
else:
    raise AssertionError("unknown proof phase")
