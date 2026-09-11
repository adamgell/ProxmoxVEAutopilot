"""Review-pending launch primitive. No cleanup or full qualification claim.

The same immutable file is bound read-only into the runner. Run --help for the
closed host interface. The --inside interface is only for that container.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import time
import uuid

PG_IMAGE = "sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"
RUNNER_IMAGE = "sha256:d615e6c8b3270ae13dc9ca95cf49aea549192b6775c1d0f1aa711457f5927f94"
IMAGE_SOURCE = "35400bd02b9816b3a0bdff0c7df3cfd20fd6d1ee"
SOCKET = "unix:///Users/Adam.Gell/.orbstack/run/docker.sock"
LABEL = "io.proxmoxveautopilot.task9-owned"
DATA = "/var/lib/postgresql/data"
RECEIPT = "/owned-linux-fixture/receipt.json"
SCRIPT = "/owned-linux-fixture/launcher.py"
GIB = 1024**3
CAPS = {"pg": 6 * GIB, "runner": 4 * GIB}
TMPFS = "rw,nosuid,nodev,size=4g,mode=0700"
# The runner image is built from this runtime source set. The launcher itself
# and evidence metadata are host-side qualification controls mounted separately
# and may advance after the immutable runner image is built.
RUNTIME_PATHS = ("rust-controller/Cargo.toml", "rust-controller/Cargo.lock",
                 "rust-controller/Dockerfile.test", "rust-controller/crates")
SMOKE = "scheduler::osdeploy::transition::tests::private_reclaim_and_repark_require_expired_exact_epoch_and_original_budget"
# These ignored entrypoints require input supplied by their supervising tests.
# The full gate still includes intentional owned-storage qualification tests.
SUPERVISED_CHILDREN = ("fixture_prefix_worker", "fixture_prefix_recovery_worker",
                       "independent_recovery_reader", "dispatching_worker_a", "recovering_worker_b")
# These ignored tests deliberately require a caller-provided empty loopback
# PostgreSQL DSN.  The owned-v1 launcher does not accept or synthesize a DSN;
# they remain a separate opt-in qualification lane.
DSN_ONLY_TESTS = ("concurrent_result_probe_conflicts_then_rolls_back_and_releases_lock",
                  "session_schema_creation_rolls_back_without_leaving_tables")
OVERRIDES = {"DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_TLS_VERIFY",
             "DOCKER_CERT_PATH", "PGHOST", "PGHOSTADDR", "PGPORT", "PGDATABASE",
             "PGUSER", "PGPASSWORD", "PGPASSFILE", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS"}


def require(ok, label="admission refused"):
    if not ok:
        raise ValueError(label)


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


def u64(value):
    require(re.fullmatch(r"0|[1-9][0-9]{0,19}", value) is not None)
    number = int(value)
    require(number <= 2**64 - 1)
    return number


def capacity(raw):
    require(len(raw) <= 8192 and raw.endswith(b"\n"))
    fields = raw[:-1].decode("ascii").split("|")
    require(len(fields) == 4 and fields[0] == "tmpfs")
    size, blocks, available = map(u64, fields[1:])
    require(size > 0 and blocks > 0 and available <= blocks)
    total, free = size * blocks, size * available
    require(total <= 2**64 - 1 and total == 4 * GIB and free >= GIB // 2)
    return {"total": total, "available": free}


def cgroup(raw, role):
    require(role in CAPS and len(raw) <= 8192 and raw.endswith(b"\n"))
    lines = raw[:-1].decode("ascii").split("\n")
    require(len(lines) >= 9)
    current, maximum, swap, swap_max = map(u64, lines[:4])
    events = {}
    for line in lines[4:]:
        fields = line.split(" ")
        require(len(fields) == 2 and fields[0] not in events)
        require(fields[0] in {"low", "high", "max", "oom", "oom_kill", "oom_group_kill", "sock_throttled"})
        events[fields[0]] = u64(fields[1])
    require({"low", "high", "max", "oom", "oom_kill"} <= events.keys())
    require(maximum == CAPS[role] and current <= maximum and swap == swap_max == 0)
    require(all(value == 0 for key, value in events.items() if key != "low"))
    return {"current": current, "maximum": maximum, "events": events}


def cgroup2_mount(raw):
    """Accept the kernel mount record even when BusyBox stat cannot name it."""
    require(len(raw) <= 8192 and raw.endswith(b"\n"))
    matches = []
    for line in raw.decode("ascii").splitlines():
        fields = line.split(" - ", 1)
        require(len(fields) == 2)
        pre, post = fields[0].split(), fields[1].split()
        if len(pre) > 4 and pre[4] == "/sys/fs/cgroup" and post and post[0] == "cgroup2":
            matches.append(line)
    require(len(matches) == 1)
    return "cgroup2"


def validate_receipt(value):
    require(type(value) is dict and set(value) == {"version", "session", "pg_id", "pg_image",
            "runner_image", "source_git_sha", "system_identifier", "marker", "netns"})
    require(type(value["version"]) is int and value["version"] == 1)
    for key, pattern in (("session", r"[0-9a-f]{32}"), ("pg_id", r"[0-9a-f]{64}"),
                         ("netns", r"net:\[[1-9][0-9]{0,19}\]")):
        require(type(value[key]) is str and re.fullmatch(pattern, value[key]) is not None)
    require(value["pg_image"] == PG_IMAGE and value["runner_image"] == RUNNER_IMAGE)
    require(value["source_git_sha"] == IMAGE_SOURCE and value["marker"] == "lf_" + value["session"])
    u64(value["system_identifier"])
    u64(value["netns"][5:-1])
    require(len(canonical(value)) <= 4096)
    return value


def profile_args(session, role, pg_id=None, receipt_path=None, script_path=None, mode="smoke"):
    require(re.fullmatch(r"[0-9a-f]{32}", session) is not None and role in CAPS)
    require(mode in ("smoke", "full", "fixture", "recovery"))
    argv = ["create", "--pull=never", "--name", "task9-" + session + "-" + role,
            "--label", LABEL + ".session=" + session, "--label", LABEL + ".role=" + role,
            "--memory", str(CAPS[role]), "--memory-swap", str(CAPS[role]),
            "--cgroupns", "private", "--ipc", "private", "--shm-size", str(64 * 1024**2),
            "--restart", "no"]
    if role == "pg":
        return argv + ["--platform", "linux/arm64", "--network", "none", "--tmpfs", DATA + ":" + TMPFS,
                       "-e", "POSTGRES_PASSWORD=local-linux-probe", PG_IMAGE,
                       "postgres", "-c", "listen_addresses=127.0.0.1", "-c", "cluster_name=lf_" + session]
    require(type(pg_id) is str and re.fullmatch(r"[0-9a-f]{64}", pg_id) is not None)
    require(receipt_path and script_path)
    return argv + ["--platform", "linux/amd64", "--network", "container:" + pg_id,
                   "--mount", "type=bind,src=" + receipt_path + ",dst=" + RECEIPT + ",readonly",
                   "--mount", "type=bind,src=" + script_path + ",dst=" + SCRIPT + ",readonly",
                   # The durability target exercises deeply nested async
                   # state on emulated amd64; keep its test-thread stack
                   # explicit so the qualification result is not host-stack
                   # dependent. This does not alter controller runtime limits.
                   "-e", "PROXMOXVEAUTOPILOT_LINUX_TEST_DB=owned-v1",
                   "-e", "RUST_MIN_STACK=16777216", "--workdir", "/workspace/rust-controller",
                   "--entrypoint", "/usr/bin/python3", RUNNER_IMAGE, "-I", "-B", SCRIPT, "--inside", mode]


def owned(row, session, role, ident, image, pg_id=None, binds=None):
    require(row["Id"] == ident and row["Name"] == "/task9-" + session + "-" + role)
    require(row["Image"] == image and row["Labels"].get(LABEL + ".session") == session
            and row["Labels"].get(LABEL + ".role") == role)
    host = row["Host"]
    require(host["Memory"] == host["MemorySwap"] == CAPS[role])
    require(host["CgroupnsMode"] == host["IpcMode"] == "private" and host["PidMode"] == "")
    require(host["ShmSize"] == 64 * 1024**2 and not host["Privileged"] and not host["OomKillDisable"])
    require(not host["PortBindings"] and not host["CapAdd"] and not host["Devices"]
            and not host["Binds"] and not host["VolumesFrom"])
    require(host["RestartPolicy"]["Name"] == "no" and row["RestartCount"] == 0)
    require(not row["State"]["OOMKilled"] and not row["State"]["Restarting"])
    require(all(value is None for value in row["Ports"].values()))
    if role == "pg":
        require(host["NetworkMode"] == "none" and host["Tmpfs"] == {DATA: TMPFS})
        require(len(row["Mounts"]) <= 1)
        require(all(m["Type"] == "tmpfs" and m["Destination"] == DATA
                    and m["RW"] is True for m in row["Mounts"]))
    else:
        require(host["NetworkMode"] == "container:" + pg_id and not host.get("Tmpfs", {}))
        actual = {(m["Source"], m["Destination"]) for m in row["Mounts"]}
        require(actual == set(binds) and len(row["Mounts"]) == len(binds))
        require(all(m["Type"] == "bind" and m["RW"] is False for m in row["Mounts"]))
    return row


def bounded(argv, seconds, cap=64 * 1024**2):
    """Cap each stream; reserve two seconds inside the bound for kill/reap.

    A failed/uncertain local CLI is not proof that its Docker workload stopped.
    Host failure recovery below checks the captured identity before killing it.
    """
    require(seconds > 2)
    deadline = time.monotonic() + seconds
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    buffers = [bytearray(), bytearray()]
    failure = None
    with selectors.DefaultSelector() as selector:
        for index, stream in enumerate((proc.stdout, proc.stderr)):
            selector.register(stream, selectors.EVENT_READ, index)
        try:
            while selector.get_map() or proc.poll() is None:
                require(time.monotonic() < deadline - 2, "child deadline")
                for key, _ in selector.select(min(.05, max(0, deadline - 2 - time.monotonic()))):
                    data = os.read(key.fileobj.fileno(), min(65536, cap + 1 - len(buffers[key.data])))
                    if not data:
                        selector.unregister(key.fileobj)
                    else:
                        buffers[key.data].extend(data)
                        require(len(buffers[key.data]) <= cap, "child output cap")
            # A successful leader with surviving descendants is not completion.
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise ValueError("child group remains")
        except (ValueError, OSError) as error:
            failure = type(error).__name__ + ": " + str(error)
        finally:
            if failure is not None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                proc.wait(timeout=max(.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                failure = "direct child reap uncertain"
            proc.stdout.close()
            proc.stderr.close()
    return {"exit": proc.returncode, "failure": failure}, bytes(buffers[0]), bytes(buffers[1])


CGROUP_FILES = ["/sys/fs/cgroup/" + name for name in
                ("memory.current", "memory.max", "memory.swap.current", "memory.swap.max", "memory.events")]


def workload(mode):
    """Closed test selections; fixture children are launched only by their parent tests."""
    require(mode in ("smoke", "full", "fixture", "recovery"), "unknown workload")
    if mode == "fixture":
        return (["cargo", "test", "--offline", "--locked", "-p", "pve-port",
                 "--features", "fixture-ipc", "--no-fail-fast", "--",
                 "--nocapture", "--test-threads=1"], 1800)
    argv = ["cargo", "test", "--offline", "--locked", "-p", "postgres-store"]
    if mode == "smoke":
        return (argv + ["--lib", SMOKE, "--", "--exact", "--nocapture", "--test-threads=1"], 180)
    if mode == "recovery":
        return (["cargo", "test", "--offline", "--locked", "-p", "operation-controller",
                 "--features", "fixture-ipc", "--test", "postgres_fixture_clone",
                 "configure_worker_death_after_", "--", "--nocapture", "--test-threads=1"], 300)
    return (argv + ["-p", "scheduler", "-p", "osdeploy-adapter", "-p", "operation-controller",
                   "--all-features", "--no-fail-fast", "--", "--include-ignored", "--nocapture", "--test-threads=1"]
            + [argument for child in (*SUPERVISED_CHILDREN, *DSN_ONLY_TESTS)
               for argument in ("--skip", child)], 1800)


def inside(mode):
    require(sys.platform == "linux" and not OVERRIDES.intersection(os.environ))
    require(not any(Path(p).exists() for p in ("/var/run/docker.sock", "/run/docker.sock", "/run/podman/podman.sock")))
    raw = Path(RECEIPT).read_bytes()
    value = validate_receipt(json.loads(raw))
    require(raw == canonical(value) and os.readlink("/proc/self/ns/net") == value["netns"])
    require(os.environ.get("CONTROLLER_GIT_SHA") == IMAGE_SOURCE)
    require(Path("/proc/self/cgroup").read_bytes() == b"0::/\n")
    cgroup(b"".join(Path(p).read_bytes() for p in CGROUP_FILES), "runner")
    argv, seconds = workload(mode)
    result, out, err = bounded(argv, seconds)
    sys.stdout.buffer.write(out)
    sys.stderr.buffer.write(err)
    print(json.dumps({"invocation_only": result, "qualification": "INCOMPLETE"}), flush=True)
    if mode == "smoke":
        require(b"test result: ok. 1 passed; 0 failed;" in out
                and ("test " + SMOKE + " ... ok").encode() in out, "exact smoke result missing")
    # The marker is forbidden for the one-test smoke proof, where it would
    # indicate that unrelated cleanup work leaked into the invocation. The
    # full suite intentionally exercises and emits that diagnostic, so its
    # authoritative pass/fail signal is the bounded Cargo exit status.
    return 1 if result["failure"] or result["exit"] != 0 or (
        mode == "smoke" and b"native_fake_cleanup_unconfirmed" in out + err
    ) else 0


INSPECT = '{"Id":{{json .Id}},"Name":{{json .Name}},"Image":{{json .Image}},"Labels":{{json .Config.Labels}},"Host":{{json .HostConfig}},"Mounts":{{json .Mounts}},"State":{{json .State}},"RestartCount":{{json .RestartCount}},"Ports":{{json .NetworkSettings.Ports}}}'
SETTINGS_SQL = "SELECT current_setting('server_version_num'), system_identifier::text, current_setting('cluster_name'), current_setting('listen_addresses'), current_setting('shared_buffers'), current_setting('max_connections'), current_setting('max_wal_size'), current_setting('min_wal_size'), current_setting('checkpoint_timeout'), current_setting('fsync'), current_setting('synchronous_commit'), current_setting('full_page_writes') FROM pg_control_system()"


def host(args):
    require(args.runner_image == "rust-controller-task9:local", "unreviewed image tag")
    root = Path(__file__).resolve().parents[2]
    require(os.statvfs(root).f_bavail * os.statvfs(root).f_frsize >= 18 * GIB, "host disk guard")
    evidence = Path(args.evidence).absolute()
    evidence.mkdir(mode=0o700, parents=False, exist_ok=False)
    session = uuid.uuid4().hex
    sequence = 0

    def run(argv, seconds=3, cap=8192):
        nonlocal sequence
        sequence += 1
        start = time.time_ns()
        result, out, err = bounded(argv, seconds, cap)
        prefix = evidence / f"{sequence:04d}"
        prefix.with_suffix(".stdout").write_bytes(out)
        prefix.with_suffix(".stderr").write_bytes(err)
        prefix.with_suffix(".json").write_bytes(canonical({"argv": argv, "start_ns": start,
            "end_ns": time.time_ns(), "bound_s": seconds, **result,
            "stdout_sha256": hashlib.sha256(out).hexdigest(), "stderr_sha256": hashlib.sha256(err).hexdigest()}))
        require(result["failure"] is None and result["exit"] == 0, "child failed; retained receipt")
        return out

    def docker(*argv, seconds=3, cap=8192):
        return run(["docker", "--host", SOCKET, *argv], seconds, cap)

    host_sha = run(["git", "-C", str(root), "rev-parse", "HEAD"]).decode().strip()
    # Do not compare the whole repository HEAD: this launcher and its retained
    # evidence are intentionally updated after the immutable runner image. The
    # runtime paths copied into the image must, however, remain byte-identical
    # to the image's source seal before any owned container is created.
    require(run(["git", "-C", str(root), "diff", "--quiet", IMAGE_SOURCE,
                 "--", *RUNTIME_PATHS]) == b"", "runtime source revision mismatch")
    require(run(["git", "-C", str(root), "diff", "HEAD", "--", "rust-controller"]) == b"", "tracked source dirty")
    pg_image = docker("image", "inspect", PG_IMAGE, "--format", "{{.Id}}|{{.Architecture}}|{{.Os}}").decode().strip().split("|")
    runner_image = docker("image", "inspect", args.runner_image, "--format", "{{.Id}}|{{.Architecture}}|{{.Os}}").decode().strip().split("|")
    require(pg_image[1:] == ["arm64", "linux"] and runner_image == [RUNNER_IMAGE, "amd64", "linux"])
    # Reject image-declared volumes before create, not after anonymous storage
    # has already been allocated. PG's sole declaration is shadowed by tmpfs.
    require(json.loads(docker("image", "inspect", PG_IMAGE, "--format", "{{json .Config.Volumes}}")) == {DATA: {}})
    require(json.loads(docker("image", "inspect", RUNNER_IMAGE, "--format", "{{json .Config.Volumes}}")) in (None, {}))
    script = Path(__file__).resolve()
    receipt_file = evidence / "receipt.json"
    binds = [(str(receipt_file), RECEIPT), (str(script), SCRIPT)]
    state = {"session": session, "launcher_head": host_sha, "image_source": IMAGE_SOURCE,
             "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(), "qualification": "INCOMPLETE"}

    def save():
        (evidence / "state.json").write_bytes(canonical(state))

    def inspect(role):
        row = json.loads(docker("inspect", state[role], "--format", INSPECT, cap=32768))
        return owned(row, session, role, state[role], pg_image[0] if role == "pg" else RUNNER_IMAGE,
                     state.get("pg"), binds)

    save()
    for role in ("pg", "runner"):
        name = "task9-" + session + "-" + role
        require(docker("ps", "-aq", "--no-trunc", "--filter", "name=^/" + name + "$") == b"", "reserved name exists")
    try:
        state["pending"] = "pg"
        save()
        raw = docker(*profile_args(session, "pg"), seconds=10)
        require(re.fullmatch(rb"[0-9a-f]{64}\n", raw) is not None)
        state["pg"] = raw.decode().strip()
        save()
        inspect("pg")
        docker("start", state["pg"], seconds=10)
        # Readiness only may be pending; failed protocol/resource reads never retry.
        ready_deadline = time.monotonic() + 30
        while True:
            ready = docker("exec", state["pg"], "sh", "-c", "pg_isready -q -h 127.0.0.1 -U postgres; printf '%s\\n' \"$?\"")
            require(ready in (b"0\n", b"1\n", b"2\n"))
            if ready == b"0\n":
                break
            require(time.monotonic() + 3.25 < ready_deadline, "readiness deadline")
            time.sleep(.25)
        inspect("pg")
        settings = docker("exec", state["pg"], "psql", "-XAt", "-U", "postgres", "-d", "postgres", "-c", SETTINGS_SQL).decode().strip().split("|")
        require(len(settings) == 12 and 160000 <= u64(settings[0]) < 170000)
        require(settings[2:] == ["lf_" + session, "127.0.0.1", "128MB", "100", "1GB", "80MB", "5min", "on", "on", "on"])
        netns = docker("exec", state["pg"], "readlink", "/proc/self/ns/net").decode().strip()
        state["cgroup_fs"] = cgroup2_mount(docker("exec", state["pg"], "grep", "-e", " - cgroup2 cgroup ", "/proc/self/mountinfo"))
        require(docker("exec", state["pg"], "cat", "/proc/self/cgroup") == b"0::/\n")
        state["capacity"] = capacity(docker("exec", state["pg"], "stat", "-f", "-c", "%T|%S|%b|%a", DATA))
        state["pg_cgroup"] = cgroup(docker("exec", state["pg"], "cat", *CGROUP_FILES), "pg")
        meminfo = docker("exec", state["pg"], "cat", "/proc/meminfo")
        available = re.findall(rb"^MemAvailable: +([0-9]+) kB$", meminfo, re.MULTILINE)
        require(len(available) == 1 and u64(available[0].decode()) * 1024 >= 12 * GIB, "VM memory guard")
        value = validate_receipt({"version": 1, "session": session, "pg_id": state["pg"],
            "pg_image": PG_IMAGE, "runner_image": RUNNER_IMAGE, "source_git_sha": IMAGE_SOURCE,
            "system_identifier": settings[1], "marker": "lf_" + session, "netns": netns})
        receipt_file.write_bytes(canonical(value))
        state["pending"] = "runner"
        save()
        # Large locally sealed images may take longer to materialize under
        # OrbStack; this is create admission only, not the runner test bound.
        raw = docker(*profile_args(session, "runner", state["pg"], str(receipt_file), str(script), args.mode), seconds=60)
        require(re.fullmatch(rb"[0-9a-f]{64}\n", raw) is not None)
        state["runner"] = raw.decode().strip()
        state.pop("pending")
        save()
        inspect("runner")
        docker("start", "--attach", state["runner"], seconds=200 if args.mode == "smoke" else 1820, cap=64 * 1024**2)
        row = inspect("runner")
        require(not row["State"]["Running"] and row["State"]["ExitCode"] == 0)
        inspect("pg")
        state["invocation"] = "passed; qualification incomplete"
    except BaseException:
        state["invocation"] = "failed; retained resources; pending identities may need manual resolution"
        # Never remove anything. A captured, re-admitted runner may be killed to
        # bound its workload; uncertain ownership leaves it untouched and fails.
        if "runner" in state:
            row = inspect("runner")
            if row["State"]["Running"]:
                docker("kill", state["runner"])
        raise
    finally:
        save()
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inside", choices=("smoke", "full", "fixture", "recovery"))
    parser.add_argument("--runner-image")
    parser.add_argument("--evidence")
    parser.add_argument("--mode", choices=("smoke", "full", "fixture", "recovery"), default="smoke")
    args = parser.parse_args()
    if args.inside:
        return inside(args.inside)
    require(args.runner_image and args.evidence, "explicit runner tag and new evidence directory required")
    return host(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, KeyError) as error:
        print("owned launch refused: " + str(error), file=sys.stderr)
        sys.exit(1)
