"""Check the one accepted synthetic fixture against its current Python producer.

Execute only the handler and JobManager.start AST nodes, with a capturing queue
and temporary log directory. Importing the web app would initialize services.
This intentionally covers one contract, not general Python/API compatibility.
"""

import ast
import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ProducerContractTest(unittest.TestCase):
    def test_current_synthetic_producer_matches_the_accepted_fixture(self):
        fixture = json.loads(
            (ROOT / "rust-controller/fixtures/jobs/synthetic-long-sleep.json").read_text()
        )
        app_path = ROOT / "autopilot-proxmox/web/app.py"
        handlers = [
            node for node in ast.walk(ast.parse(app_path.read_text()))
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_enqueue_test_long_sleep"
        ]
        self.assertEqual(len(handlers), 1)
        handler = handlers[0]
        handler.decorator_list = []

        jobs_path = ROOT / "autopilot-proxmox/web/jobs.py"
        manager = next(
            node for node in ast.parse(jobs_path.read_text()).body
            if isinstance(node, ast.ClassDef) and node.name == "JobManager"
        )
        start = next(
            node for node in manager.body
            if isinstance(node, ast.FunctionDef) and node.name == "start"
        )
        captured = []

        def enqueue(**fields):
            captured.append(fields)
            return {"created_at": "2026-09-04T12:00:00Z"}

        namespace = {
            "os": os,
            "jobs_db": SimpleNamespace(enqueue=enqueue),
            "PLAYBOOK_DIR": Path("/app/playbooks"),
            "Form": lambda default: default,
        }
        module = ast.Module(body=[start, handler], type_ignores=[])
        exec(compile(module, "synthetic-producer-contract", "exec"), namespace)
        with tempfile.TemporaryDirectory(prefix="rust-producer-contract-") as directory:
            instance = SimpleNamespace(jobs_dir=directory, _generate_id=lambda: fixture["id"])
            namespace["job_manager"] = SimpleNamespace(
                start=lambda *args, **kwargs: namespace["start"](instance, *args, **kwargs)
            )
            result = asyncio.run(namespace["_enqueue_test_long_sleep"](
                duration=fixture["args"]["duration"]
            ))
        self.assertEqual(result, {"id": fixture["id"]})
        self.assertEqual(captured, [{
            "job_id": fixture["id"],
            "job_type": fixture["job_type"],
            "playbook": fixture["playbook"],
            "cmd": fixture["cmd"],
            "args": fixture["args"],
        }])

    def test_queue_insert_and_row_projection_preserve_fixture_fields(self):
        fixture = json.loads(
            (ROOT / "rust-controller/fixtures/jobs/synthetic-long-sleep.json").read_text()
        )
        path = ROOT / "autopilot-proxmox/web/jobs_pg.py"
        definitions = {
            node.name: node for node in ast.parse(path.read_text()).body
            if isinstance(node, ast.FunctionDef)
        }
        statements = [
            node.value for node in ast.walk(definitions["enqueue"])
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and "INSERT INTO jobs" in node.value
        ]
        self.assertEqual(len(statements), 1)
        self.assertEqual(" ".join(statements[0].split()),
            "INSERT INTO jobs (id, job_type, playbook, cmd_json, args_json, status, created_at) "
            "VALUES (%s, %s, %s, %s, %s, 'pending', %s) RETURNING *")
        self.assertEqual(fixture["status"], "pending")
        module = ast.Module(body=[definitions[name] for name in (
            "_json_value", "_iso", "_row_to_dict"
        )], type_ignores=[])
        namespace = {"Any": object, "Jsonb": type("UnusedJsonb", (), {})}
        exec(compile(module, "synthetic-row-contract", "exec"), namespace)
        row = {**fixture, "cmd_json": fixture["cmd"], "args_json": fixture["args"]}
        del row["cmd"], row["args"]
        projected = namespace["_row_to_dict"](row)
        self.assertEqual({key: projected[key] for key in fixture}, fixture)


if __name__ == "__main__":
    unittest.main()
