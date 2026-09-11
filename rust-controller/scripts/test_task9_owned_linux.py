import unittest
import copy
import sys
import time

import task9_owned_linux as gate


class CapacityTests(unittest.TestCase):
    def test_exact_boundary(self):
        self.assertEqual(gate.capacity(b"tmpfs|4096|1048576|131072\n"),
                         {"total": 4294967296, "available": 536870912})

    def test_reserve_and_malformed_records_refused(self):
        for raw in (b"tmpfs|4096|1048576|131071\n", b"ext4|4096|1048576|131072\n",
                    b"tmpfs|4096|1048576|1048577\n", b"tmpfs|4096|1048576|131072",
                    b"tmpfs|4096|1048576|0131072\n", b"tmpfs|4096|1048576|131072\n\n",
                    b"tmpfs|18446744073709551615|1048576|131072\n"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                gate.capacity(raw)


class ContractTests(unittest.TestCase):
    def test_start_pe_workload_is_feature_gated_exact_and_bounded(self):
        argv, seconds = gate.workload("start-pe")
        self.assertEqual(seconds, 300)
        self.assertEqual(argv, ["cargo", "test", "--offline", "--locked", "-p", "postgres-store",
                               "--features", "fixture-ipc", "--test", "osdeploy_durability",
                               gate.START_PE, "--", "--exact", "--nocapture", "--test-threads=1"])
        runner = gate.profile_args("a" * 32, "runner", "b" * 64, "/receipt", "/script", "start-pe")
        self.assertEqual(runner[-3:], [gate.SCRIPT, "--inside", "start-pe"])

    def test_fixture_workload_preserves_ignored_child_entrypoints(self):
        argv, seconds = gate.workload("fixture")
        self.assertEqual(argv, ["cargo", "test", "--offline", "--locked", "-p", "pve-port",
                               "--features", "fixture-ipc", "--no-fail-fast", "--",
                               "--nocapture", "--test-threads=1"])
        self.assertEqual(seconds, 1800)
        self.assertNotIn("--include-ignored", argv)
        self.assertNotIn("--ignored", argv)
        runner = gate.profile_args("a" * 32, "runner", "b" * 64, "/receipt", "/script", "fixture")
        self.assertEqual(runner[-3:], [gate.SCRIPT, "--inside", "fixture"])
        self.assertIn(gate.RUNNER_IMAGE, runner)
        with self.assertRaises(ValueError):
            gate.workload("arbitrary")

    def test_existing_workloads_keep_their_selection_and_bounds(self):
        smoke, seconds = gate.workload("smoke")
        self.assertEqual(seconds, 180)
        self.assertIn(gate.SMOKE, smoke)
        self.assertIn("--exact", smoke)
        full, seconds = gate.workload("full")
        self.assertEqual(seconds, 1800)
        self.assertIn("--include-ignored", full)
        self.assertNotIn("pve-port", full)
        skips = [full[i + 1] for i, value in enumerate(full) if value == "--skip"]
        self.assertEqual(skips, ["fixture_prefix_worker", "fixture_prefix_recovery_worker",
                                 "fixture_start_response_reload_worker",
                                 "independent_recovery_reader", "dispatching_worker_a", "recovering_worker_b",
                                 "concurrent_result_probe_conflicts_then_rolls_back_and_releases_lock",
                                 "session_schema_creation_rolls_back_without_leaving_tables"])
        self.assertFalse(any("owned_tmpfs" in value for value in skips))

    def test_recovery_workload_is_narrow_and_bounded(self):
        argv, seconds = gate.workload("recovery")
        self.assertEqual(seconds, 300)
        self.assertIn("operation-controller", argv)
        self.assertIn("postgres_fixture_clone", argv)
        self.assertIn("configure_worker_death_after_", argv)
        self.assertNotIn("--include-ignored", argv)

    def receipt(self):
        return {"version": 1, "session": "a" * 32, "pg_id": "b" * 64,
                "pg_image": gate.PG_IMAGE, "runner_image": gate.RUNNER_IMAGE,
                "source_git_sha": gate.IMAGE_SOURCE, "system_identifier": "123",
                "marker": "lf_" + "a" * 32, "netns": "net:[123]"}

    def test_receipt_exact_shape_and_source_separation(self):
        value = self.receipt()
        self.assertEqual(gate.validate_receipt(value), value)
        for key, replacement in (("version", True), ("pg_id", "b" * 12),
                                 ("runner_image", "tag:latest"), ("source_git_sha", "f" * 40),
                                 ("system_identifier", "01"), ("marker", "lf_wrong"),
                                 ("netns", "net:[0]"), ("extra", 1)):
            broken = dict(value, **{key: replacement})
            with self.subTest(key=key), self.assertRaises(ValueError):
                gate.validate_receipt(broken)

    def test_resource_profile_and_mounts_are_closed(self):
        pg = gate.profile_args("a" * 32, "pg")
        self.assertEqual(pg[pg.index("--memory") + 1], str(6 * gate.GIB))
        self.assertEqual(pg[pg.index("--memory-swap") + 1], str(6 * gate.GIB))
        self.assertEqual(pg[pg.index("--shm-size") + 1], str(64 * 1024**2))
        self.assertIn(gate.DATA + ":" + gate.TMPFS, pg)
        self.assertNotIn("-p", pg)
        runner = gate.profile_args("a" * 32, "runner", "b" * 64, "/receipt", "/script")
        self.assertEqual(runner[runner.index("--network") + 1], "container:" + "b" * 64)
        self.assertEqual(runner.count("--mount"), 2)
        self.assertIn("PROXMOXVEAUTOPILOT_LINUX_TEST_DB=owned-v1", runner)
        self.assertEqual(runner[-3:], [gate.SCRIPT, "--inside", "smoke"])
        self.assertEqual(gate.profile_args("a" * 32, "runner", "b" * 64, "/receipt", "/script", "full")[-1], "full")
        for session in ("", "A" * 32, "a" * 31, "a" * 32 + ";"):
            with self.assertRaises(ValueError):
                gate.profile_args(session, "pg")

    def test_cgroup_exact_caps_and_pressure_refusal(self):
        for role in gate.CAPS:
            raw = (f"10\n{gate.CAPS[role]}\n0\n0\nlow 0\nhigh 0\nmax 0\noom 0\noom_kill 0\nsock_throttled 0\n").encode()
            self.assertEqual(gate.cgroup(raw, role)["current"], 10)
            for broken in (raw.replace(b"oom 0", b"oom 1"), raw + b"oom 0\n",
                           raw.replace(b"high 0\n", b""), raw.replace(b"max 0", b"max 1"),
                           raw + b"new_kernel_key 0\n", raw[:-1],
                           raw.replace(b"\n0\n0\n", b"\n1\n0\n")):
                with self.subTest(role=role, broken=broken), self.assertRaises(ValueError):
                    gate.cgroup(broken, role)

    def test_cgroup2_mount_uses_kernel_mount_record(self):
        raw = b"32023 32022 0:32 / /sys/fs/cgroup ro,nosuid - cgroup2 cgroup rw,nsdelegate\n"
        self.assertEqual(gate.cgroup2_mount(raw), "cgroup2")
        for broken in (b"32023 32022 0:32 / /other ro - cgroup2 cgroup rw\n",
                       b"malformed\n", b"32023 32022 0:32 / /sys/fs/cgroup ro - tmpfs tmpfs rw\n"):
            with self.subTest(broken=broken), self.assertRaises(ValueError):
                gate.cgroup2_mount(broken)

    def test_ownership_mismatch_never_admitted(self):
        host = {"Memory": 6 * gate.GIB, "MemorySwap": 6 * gate.GIB,
                "CgroupnsMode": "private", "IpcMode": "private", "PidMode": "",
                "ShmSize": 64 * 1024**2, "Privileged": False, "OomKillDisable": False,
                "PortBindings": {}, "CapAdd": [], "Devices": [], "Binds": [],
                "VolumesFrom": [], "RestartPolicy": {"Name": "no"},
                "NetworkMode": "none", "Tmpfs": {gate.DATA: gate.TMPFS}}
        row = {"Id": "b" * 64, "Name": "/task9-" + "a" * 32 + "-pg", "Image": gate.PG_IMAGE,
               "Labels": {gate.LABEL + ".session": "a" * 32, gate.LABEL + ".role": "pg"},
               "Host": host, "Mounts": [], "RestartCount": 0,
               "State": {"OOMKilled": False, "Restarting": False}, "Ports": {}}
        gate.owned(row, "a" * 32, "pg", "b" * 64, gate.PG_IMAGE)
        for section, key, replacement in ((None, "Id", "c" * 64), (None, "Image", "wrong"),
                (None, "Name", "/foreign"), ("Host", "MemorySwap", -1),
                ("Host", "CgroupnsMode", "host"), ("Host", "Privileged", True),
                ("Host", "Binds", ["/host:/guest"]), ("State", "OOMKilled", True)):
            broken = copy.deepcopy(row)
            (broken if section is None else broken[section])[key] = replacement
            with self.subTest(key=key), self.assertRaises(ValueError):
                gate.owned(broken, "a" * 32, "pg", "b" * 64, gate.PG_IMAGE)


class SupervisorTests(unittest.TestCase):
    def test_success_retains_both_streams(self):
        result, out, err = gate.bounded([sys.executable, "-c", "import sys; print('out'); print('err',file=sys.stderr)"], 3)
        self.assertEqual(result, {"exit": 0, "failure": None})
        self.assertEqual((out, err), (b"out\n", b"err\n"))

    def test_deadline_and_output_cap_are_fatal_and_finite(self):
        for code, cap in (("import time; time.sleep(30)", 1024), ("print('x'*10000)", 32)):
            start = time.monotonic()
            result, out, _ = gate.bounded([sys.executable, "-c", code], 3, cap)
            self.assertIsNotNone(result["failure"])
            self.assertIsNotNone(result["exit"])
            self.assertLess(time.monotonic() - start, 3)
            self.assertLessEqual(len(out), cap + 1)

    def test_descendant_pipe_after_leader_exit_is_not_success(self):
        code = "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])"
        result, _, _ = gate.bounded([sys.executable, "-c", code], 3)
        self.assertIsNotNone(result["failure"])


if __name__ == "__main__":
    unittest.main()
