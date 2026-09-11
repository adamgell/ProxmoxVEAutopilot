"""Synthetic protocol/SQL lifecycle tests; only standard library required."""
import unittest
import copy
import io
import json
import stat
from types import SimpleNamespace
from unittest.mock import patch
import linux_postgres as fixture

SESSION = "a" * 32
NONCE = "b" * 32
NAME = "lf_native_test_" + NONCE
MARKER = "lf:v1:" + SESSION + ":native_test:" + NONCE
REQUEST = {"database": NAME, "marker": MARKER, "expected_oid": 42,
           "receipt": {"session": SESSION}, "nonce": NONCE}
GOOD = '{"marker":"lf_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","netns":"net:[12345]","pg_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","pg_image":"sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777","runner_image":"sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","session":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","source_git_sha":"dddddddddddddddddddddddddddddddddddddddd","system_identifier":"7682173388088000546","version":1}'
RECEIPT = json.loads(GOOD)
FULL_REQUEST = dict(REQUEST, receipt=RECEIPT, family="native_test", version=1, budget_ms=2800)

class Cursor:
    def __init__(self, row=None, locked=True):
        self.row, self.locked, self.result = row, locked, None
        self.writes = []

    def execute(self, query, params=None):
        query = str(query)
        if "pg_try_advisory_lock" in query:
            assert params == ("lf:v1:" + SESSION + ":" + NONCE,)
            self.result = (self.locked,)
        elif "FROM pg_database" in query:
            assert params == (NAME,)
            self.result = self.row
        elif query.startswith("DROP DATABASE"):
            assert query == 'DROP DATABASE "' + NAME + '" WITH (FORCE)'
            self.writes.append(query)
            self.row = None
        elif query.startswith("CREATE DATABASE"):
            assert query == 'CREATE DATABASE "' + NAME + '" TEMPLATE template0'
            self.writes.append(query)
            self.row = (42, None)
        elif query.startswith("COMMENT ON DATABASE"):
            assert query == 'COMMENT ON DATABASE "' + NAME + '" IS %s'
            assert params == (MARKER,)
            self.writes.append(query)
            self.row = (42, params[0])
        else:
            raise AssertionError("unexpected query " + query)

    def fetchone(self):
        return self.result

class Budget:
    def execute(self, cursor, query, params=None):
        cursor.execute(query, params)

class SQL:
    @staticmethod
    def SQL(value):
        return value

    @staticmethod
    def Identifier(value):
        return '"' + value + '"'

class LifecycleTests(unittest.TestCase):
    def test_foreign_unmarked_and_matching_marker_wrong_oid_are_retained(self):
        for row in [(42, "foreign"), (42, None), (43, MARKER)]:
            with self.subTest(row=row):
                cursor = Cursor(row)
                with self.assertRaises(fixture.Refused):
                    fixture.cleanup(cursor, REQUEST, Budget(), SQL)
                self.assertEqual(cursor.writes, [])

    def test_active_create_lock_prevents_even_absence_confirmation(self):
        cursor = Cursor(None, locked=False)
        with self.assertRaises(fixture.Refused):
            fixture.cleanup(cursor, REQUEST, Budget(), SQL)
        self.assertEqual(cursor.writes, [])

    def test_lost_response_requires_complete_marker_before_recovering_oid(self):
        for expected in [None, 42]:
            cursor = Cursor((42, MARKER))
            result = fixture.cleanup(cursor, dict(REQUEST, expected_oid=expected), Budget(), SQL)
            self.assertEqual(result, {"database": NAME, "status": "absent", "version": 1})
            self.assertIsNone(cursor.row)
            self.assertEqual(cursor.writes, ['DROP DATABASE "' + NAME + '" WITH (FORCE)'])
        cursor = Cursor(None)
        self.assertEqual(fixture.cleanup(cursor, REQUEST, Budget(), SQL)["status"], "absent")
        self.assertEqual(cursor.writes, [])

    def test_create_never_adopts_present_name_or_busy_lock(self):
        for row, locked in [(None, False), ((42, MARKER), True), ((42, None), True)]:
            cursor = Cursor(row, locked)
            with self.assertRaises(fixture.Refused):
                fixture.create(cursor, REQUEST, Budget(), SQL)
            self.assertEqual(cursor.writes, [])

    def test_create_stamps_and_confirms_oid_before_success(self):
        cursor = Cursor()
        result = fixture.create(cursor, REQUEST, Budget(), SQL)
        self.assertEqual(result, {"database": NAME, "marker": MARKER, "oid": 42, "version": 1})
        self.assertEqual(cursor.row, (42, MARKER))
        self.assertEqual(len(cursor.writes), 2)

    def test_cleanup_cannot_report_success_until_absence_is_observed(self):
        class StillPresent(Cursor):
            def execute(self, query, params=None):
                if str(query).startswith("DROP DATABASE"):
                    self.writes.append(str(query))
                else:
                    super().execute(query, params)
        cursor = StillPresent((42, MARKER))
        with self.assertRaises(fixture.Refused):
            fixture.cleanup(cursor, REQUEST, Budget(), SQL)
        self.assertEqual(cursor.row, (42, MARKER))
        self.assertEqual(len(cursor.writes), 1)

    def test_create_does_not_confirm_replaced_or_unstamped_identity(self):
        class ChangedOnComment(Cursor):
            def execute(self, query, params=None):
                super().execute(query, params)
                if str(query).startswith("COMMENT ON DATABASE"):
                    self.row = (43, MARKER)
        cursor = ChangedOnComment()
        with self.assertRaises(fixture.Refused):
            fixture.create(cursor, REQUEST, Budget(), SQL)
        self.assertEqual(cursor.row, (43, MARKER))

    def test_create_fault_boundaries_preserve_recovery_distinctions(self):
        for stage, expected in [("before_create", None), ("unmarked", (42, None)), ("stamped", (42, MARKER))]:
            cursor = Cursor()
            def checkpoint(actual):
                if actual == stage:
                    raise fixture.Refused()
            with self.assertRaises(fixture.Refused):
                fixture.create(cursor, REQUEST, Budget(), SQL, checkpoint)
            self.assertEqual(cursor.row, expected)
            if stage == "unmarked":
                with self.assertRaises(fixture.Refused):
                    fixture.cleanup(cursor, dict(REQUEST, expected_oid=None), Budget(), SQL)
                self.assertEqual(cursor.row, (42, None))
            else:
                fixture.cleanup(cursor, dict(REQUEST, expected_oid=None), Budget(), SQL)
                self.assertIsNone(cursor.row)

class ProtocolTests(unittest.TestCase):
    def test_fault_checkpoint_uses_original_remaining_budget_and_never_resumes_create(self):
        output = io.StringIO()
        with patch.object(fixture.time, "monotonic", return_value=10.75), patch.object(fixture.time, "sleep") as sleep, patch.object(fixture.sys, "stdout", output):
            with self.assertRaises(fixture.Refused):
                fixture._fault_checkpoint("unmarked", "unmarked", 11.0)
            sleep.assert_called_once_with(0.25)
        self.assertEqual(output.getvalue(), '{"stage":"unmarked","version":1}\n')

    def test_exhausted_fault_budget_never_waits_or_announces_readiness(self):
        for stage in ["locked", "unmarked"]:
            for deadline in [9.0, 10.0]:
                output = io.StringIO()
                with patch.object(fixture.time, "monotonic", return_value=10.0), patch.object(fixture.time, "sleep") as sleep, patch.object(fixture.sys, "stdout", output):
                    with self.assertRaises(fixture.Refused):
                        fixture._fault_checkpoint(stage, "unmarked", deadline)
                    sleep.assert_not_called()
                self.assertEqual(output.getvalue(), "")

    def test_canonical_receipt_matches_independent_rust_literal(self):
        self.assertEqual(fixture.encode(fixture.receipt(fixture.decode(GOOD))), GOOD)
        self.assertEqual(fixture.decode(GOOD + "\n"), RECEIPT)
        for bad in [" " + GOOD, GOOD + "\n\n", GOOD + "\r\n", GOOD.replace('"version":1', '"version":1,"version":1'), GOOD.replace("marker", "mar\\u006ber"), "x" * 8193]:
            with self.subTest(bad=bad[:60]), self.assertRaises((fixture.Refused, ValueError)):
                fixture.decode(bad)

    def test_receipt_rejects_schema_types_hashes_namespace_and_overflow(self):
        for key in RECEIPT:
            value = copy.deepcopy(RECEIPT)
            del value[key]
            with self.subTest(missing=key), self.assertRaises(fixture.Refused):
                fixture.receipt(value)
            for bad in [None, [], {}, True]:
                value = dict(RECEIPT, **{key: bad})
                with self.subTest(key=key, bad=bad), self.assertRaises(fixture.Refused):
                    fixture.receipt(value)
        for key, bad in [("version", 1.0), ("version", 2), ("session", "A" * 32), ("pg_id", "b" * 63), ("pg_image", "postgres:16-alpine"), ("runner_image", "sha256:foreign"), ("source_git_sha", "d" * 39), ("marker", "lf_foreign"), ("netns", "net:[0]"), ("netns", "net:[01]"), ("netns", "net:[18446744073709551616]"), ("system_identifier", "18446744073709551616"), ("system_identifier", "01"), ("system_identifier", "-1")]:
            with self.subTest(key=key, bad=bad), self.assertRaises(fixture.Refused):
                fixture.receipt(dict(RECEIPT, **{key: bad}))
        with self.assertRaises(fixture.Refused):
            fixture.receipt(dict(RECEIPT, endpoint="127.0.0.1"))

    def test_requests_are_exact_bounded_and_cannot_configure_connections(self):
        self.assertEqual(fixture.request("admit", '{"budget_ms":2800,"version":1}'), {"budget_ms": 2800, "version": 1})
        for operation in ["create", "cleanup"]:
            value = dict(FULL_REQUEST, expected_oid=None)
            self.assertEqual(fixture.request(operation, fixture.encode(value)), value)
            for key in value:
                bad = dict(value)
                del bad[key]
                with self.subTest(operation=operation, missing=key), self.assertRaises(fixture.Refused):
                    fixture.request(operation, fixture.encode(bad))
            for key, bad_value in [("family", "postgres"), ("nonce", "../foreign"), ("database", "postgres"), ("marker", "foreign"), ("expected_oid", 0), ("expected_oid", True), ("expected_oid", 2**32), ("budget_ms", 0), ("budget_ms", 3001), ("budget_ms", True), ("version", 1.0), ("host", "foreign"), ("fault", "stamped")]:
                with self.subTest(key=key), self.assertRaises(fixture.Refused):
                    fixture.request(operation, fixture.encode(dict(value, **{key: bad_value})))
        with self.assertRaises(fixture.Refused):
            fixture.request("create", fixture.encode(FULL_REQUEST))
        with self.assertRaises(fixture.Refused):
            fixture.request("foreign", '{"budget_ms":2800,"version":1}')

class AdmissionTests(unittest.TestCase):
    def test_file_namespace_socket_and_overrides_are_checked_before_sql(self):
        raw = (GOOD + "\n").encode("ascii")
        with patch.object(fixture.os, "environ", {}), patch.object(fixture.os.path, "lexists", return_value=False) as exists, patch.object(fixture.os, "lstat", return_value=SimpleNamespace(st_mode=stat.S_IFDIR)), patch.object(fixture.os, "open", return_value=7) as opened, patch.object(fixture.os, "fstat", return_value=SimpleNamespace(st_mode=stat.S_IFREG, st_size=len(raw))) as metadata, patch.object(fixture.os, "read", return_value=raw), patch.object(fixture.os, "close"), patch.object(fixture.os, "readlink", return_value="net:[12345]") as namespace:
            self.assertEqual(fixture.inspect_receipt(), RECEIPT)
            self.assertEqual(opened.call_args.args[0], "/owned-linux-fixture/receipt.json")
            self.assertTrue(opened.call_args.args[1] & fixture.os.O_NOFOLLOW)
            namespace.return_value = "net:[999]"
            with self.assertRaises(fixture.Refused):
                fixture.inspect_receipt()
            namespace.return_value = "net:[12345]"
            for kind, size in [(stat.S_IFLNK, len(raw)), (stat.S_IFIFO, 0), (stat.S_IFREG, 4097)]:
                metadata.return_value = SimpleNamespace(st_mode=kind, st_size=size)
                with self.assertRaises(fixture.Refused):
                    fixture.inspect_receipt()
            exists.return_value = True
            with self.assertRaises(fixture.Refused):
                fixture.inspect_receipt()
        for key in fixture.OVERRIDES:
            with patch.object(fixture.os, "environ", {key: ""}), self.assertRaises(fixture.Refused):
                fixture.inspect_receipt()

    def test_changed_receipt_refuses_before_driver_import_or_connection(self):
        for key, value in [("netns", "net:[999]"), ("pg_id", "e" * 64)]:
            with patch.object(fixture, "inspect_receipt", return_value=dict(RECEIPT, **{key: value})), self.assertRaises(fixture.Refused):
                fixture.run("cleanup", FULL_REQUEST)

    def test_instance_version_system_identifier_and_marker_must_all_match(self):
        for row in [(160004, RECEIPT["system_identifier"], RECEIPT["marker"]), (170001, RECEIPT["system_identifier"], RECEIPT["marker"]), (160004, "1", RECEIPT["marker"]), (160004, RECEIPT["system_identifier"], "foreign")]:
            cursor = SimpleNamespace(execute=lambda *args: None, fetchone=lambda: row)
            if row[0] == 160004 and row[1:] == (RECEIPT["system_identifier"], RECEIPT["marker"]):
                fixture.verify_instance(cursor, RECEIPT, Budget())
            else:
                with self.assertRaises(fixture.Refused):
                    fixture.verify_instance(cursor, RECEIPT, Budget())

    def test_budget_cannot_start_connect_below_libpq_minimum(self):
        calls = []
        driver = SimpleNamespace(connect=lambda **kwargs: calls.append(kwargs))
        with patch.object(fixture.time, "monotonic", return_value=10):
            budget = fixture.Budget(2800)
            budget.connect(driver)
        self.assertEqual(calls[0]["host"], "127.0.0.1")
        self.assertEqual(calls[0]["hostaddr"], "127.0.0.1")
        self.assertEqual(calls[0]["port"], 5432)
        self.assertEqual(calls[0]["dbname"], "postgres")
        self.assertEqual(calls[0]["connect_timeout"], 2)
        self.assertEqual(calls[0]["passfile"], "/dev/null")
        with patch.object(fixture.time, "monotonic", return_value=11):
            with self.assertRaises(fixture.Refused):
                budget.connect(driver)
        self.assertEqual(len(calls), 1)
        with patch.object(fixture.time, "monotonic", return_value=13), self.assertRaises(fixture.Refused):
            budget.milliseconds()

if __name__ == "__main__":
    unittest.main()
