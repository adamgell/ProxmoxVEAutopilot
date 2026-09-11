"""Fixed private owned Linux fixture protocol (embedded by Rust).

No caller-controlled endpoint, SQL, executable, path or fault selection.
The host independently verifies the receipt bind and immutable runner.
"""
import json
import os
import re
import stat
import sys
import time

PG_IMAGE = "sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"
RECEIPT_PATH = "/owned-linux-fixture/receipt.json"
RECEIPT_KEYS = {"marker", "netns", "pg_id", "pg_image", "runner_image", "session",
                "source_git_sha", "system_identifier", "version"}
FAMILIES = {"native_proof", "native_test", "osdeploy_registration_test", "rust_controller_test"}
OVERRIDES = {"DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_TLS_VERIFY",
             "DOCKER_CERT_PATH", "PGHOST", "PGHOSTADDR", "PGPORT", "PGDATABASE",
             "PGUSER", "PGPASSWORD", "PGPASSFILE", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS"}
SOCKETS = ("/var/run/docker.sock", "/run/docker.sock", "/run/podman/podman.sock")

class Refused(Exception):
    """Deliberately contains no SQL, path contents, credentials or driver error."""

def require(condition):
    if not condition:
        raise Refused()

def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

def unique(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value)
        value[key] = item
    return value

def decode(raw, limit=8192):
    require(type(raw) is str and len(raw.encode("utf-8")) <= limit)
    text = raw[:-1] if raw.endswith("\n") else raw
    value = json.loads(text, object_pairs_hook=unique)
    require(encode(value) == text)
    return value

def exact(value, keys):
    require(type(value) is dict and set(value) == set(keys))

def integer(value, minimum, maximum):
    require(type(value) is int and minimum <= value <= maximum)

def matches(value, pattern):
    return type(value) is str and re.fullmatch(pattern, value, flags=re.ASCII) is not None

def decimal(value, minimum=0):
    require(matches(value, r"0|[1-9][0-9]{0,19}"))
    integer(int(value), minimum, 2**64 - 1)

def receipt(value):
    exact(value, RECEIPT_KEYS)
    integer(value["version"], 1, 1)
    require(len(encode(value)) <= 4096)
    require(matches(value["session"], r"[0-9a-f]{32}"))
    require(matches(value["pg_id"], r"[0-9a-f]{64}"))
    require(value["pg_image"] == PG_IMAGE)
    require(matches(value["runner_image"], r"sha256:[0-9a-f]{64}"))
    require(matches(value["source_git_sha"], r"[0-9a-f]{40}"))
    decimal(value["system_identifier"])
    require(value["marker"] == "lf_" + value["session"])
    require(matches(value["netns"], r"net:\[[1-9][0-9]{0,19}\]"))
    decimal(value["netns"][5:-1], 1)
    return value

def request(operation, raw):
    require(operation in {"admit", "create", "cleanup"})
    value = decode(raw)
    names = {"version", "budget_ms"}
    if operation != "admit":
        names |= {"receipt", "family", "nonce", "database", "marker", "expected_oid"}
    exact(value, names)
    integer(value["version"], 1, 1)
    integer(value["budget_ms"], 1, 3000)
    if operation != "admit":
        receipt(value["receipt"])
        require(type(value["family"]) is str and value["family"] in FAMILIES)
        require(matches(value["nonce"], r"[0-9a-f]{32}"))
        require(value["database"] == "lf_" + value["family"] + "_" + value["nonce"])
        require(len(value["database"]) <= 63)
        require(value["marker"] == "lf:v1:" + value["receipt"]["session"] + ":" + value["family"] + ":" + value["nonce"])
        if value["expected_oid"] is not None:
            integer(value["expected_oid"], 1, 2**32 - 1)
        if operation == "create":
            require(value["expected_oid"] is None)
    return value

def inspect_receipt():
    require(not OVERRIDES.intersection(os.environ))
    require(not any(os.path.lexists(path) for path in SOCKETS))
    require(stat.S_ISDIR(os.lstat("/owned-linux-fixture").st_mode))
    fd = os.open(RECEIPT_PATH, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(fd)
        require(stat.S_ISREG(metadata.st_mode) and 0 < metadata.st_size <= 4096)
        raw = os.read(fd, 4097)
        require(len(raw) == metadata.st_size and len(raw) <= 4096)
        value = receipt(decode(raw.decode("ascii"), 4096))
    finally:
        os.close(fd)
    require(value["netns"] == os.readlink("/proc/self/ns/net"))
    return value

class Budget:
    def __init__(self, milliseconds):
        self.deadline = time.monotonic() + milliseconds / 1000

    def milliseconds(self):
        remaining = int((self.deadline - time.monotonic()) * 1000)
        require(remaining > 0)
        return remaining

    def connect(self, driver):
        # libpq's smallest effective connect_timeout is two seconds.
        remaining = self.milliseconds()
        require(remaining >= 2000)
        return driver.connect(host="127.0.0.1", hostaddr="127.0.0.1", port=5432,
                              dbname="postgres", user="postgres", password="local-linux-probe",
                              connect_timeout=remaining // 1000,
                              options="-c statement_timeout=" + str(remaining) + " -c lock_timeout=" + str(remaining),
                              sslmode="disable", passfile="/dev/null")

    def execute(self, cursor, query, params=None):
        remaining = self.milliseconds()
        cursor.execute("SELECT set_config('statement_timeout', %s, false), set_config('lock_timeout', %s, false)",
                       (str(remaining), str(remaining)))
        self.milliseconds()
        cursor.execute(query, params)

def verify_instance(cursor, admitted, budget):
    budget.execute(cursor, "SELECT current_setting('server_version_num')::int, system_identifier::text, current_setting('cluster_name') FROM pg_control_system()")
    row = cursor.fetchone()
    require(row is not None and 160000 <= row[0] < 170000
            and row[1] == admitted["system_identifier"] and row[2] == admitted["marker"])

def lock(cursor, value, budget):
    budget.execute(cursor, "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
                   ("lf:v1:" + value["receipt"]["session"] + ":" + value["nonce"],))
    require(cursor.fetchone() == (True,))

def lookup(cursor, value, budget):
    budget.execute(cursor, "SELECT oid, shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = %s",
                   (value["database"],))
    return cursor.fetchone()

def create(cursor, value, budget, sql, checkpoint=lambda stage: None):
    lock(cursor, value, budget)
    checkpoint("locked")
    require(lookup(cursor, value, budget) is None)
    checkpoint("before_create")
    budget.execute(cursor, sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(value["database"])))
    checkpoint("unmarked")
    row = lookup(cursor, value, budget)
    require(row is not None)
    integer(row[0], 1, 2**32 - 1)
    require(row[1] is None)
    budget.execute(cursor, sql.SQL("COMMENT ON DATABASE {} IS %s").format(sql.Identifier(value["database"])), (value["marker"],))
    require(lookup(cursor, value, budget) == (row[0], value["marker"]))
    checkpoint("stamped")
    return {"database": value["database"], "marker": value["marker"], "oid": row[0], "version": 1}

def cleanup(cursor, value, budget, sql):
    lock(cursor, value, budget)
    row = lookup(cursor, value, budget)
    if row is not None:
        integer(row[0], 1, 2**32 - 1)
        require(row[1] == value["marker"])
        require(value["expected_oid"] is None or row[0] == value["expected_oid"])
        budget.execute(cursor, sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(value["database"])))
        require(lookup(cursor, value, budget) is None)
    return {"database": value["database"], "status": "absent", "version": 1}

def _fault_checkpoint(stage, expected, deadline):
    """Closed Linux-test checkpoint, sharing its original work deadline.

    Never resume CREATE after the hold: expiration preserves the selected
    failure state even if the Rust test has not yet dropped its child guard.
    This helper is unreachable from the normal command-line protocol.
    """
    stages = {"locked", "before_create", "unmarked", "stamped"}
    require(stage in stages and expected in stages)
    remaining = deadline - time.monotonic()
    require(remaining > 0)
    if stage != expected:
        return
    print(encode({"stage": stage, "version": 1}), flush=True)
    time.sleep(remaining)
    raise Refused()

def run(operation, value, checkpoint=lambda stage: None):
    budget = Budget(value["budget_ms"])
    admitted = inspect_receipt()
    if operation != "admit":
        require(admitted == value["receipt"])
    # Synthetic tests do not import or require the driver.
    import psycopg2
    from psycopg2 import sql
    connection = budget.connect(psycopg2)
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            verify_instance(cursor, admitted, budget)
            if operation == "admit":
                return {"receipt": admitted, "version": 1}
            if operation == "create":
                return create(cursor, value, budget, sql, checkpoint)
            return cleanup(cursor, value, budget, sql)
    finally:
        connection.close()

def main():
    try:
        require(len(sys.argv) == 3)
        value = request(sys.argv[1], sys.argv[2])
        result = run(sys.argv[1], value)
        print(encode(result), flush=True)
    except Exception:
        print("local_linux_fixture_refused", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
