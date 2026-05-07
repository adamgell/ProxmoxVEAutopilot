from __future__ import annotations

import ast
from pathlib import Path


def test_runtime_web_modules_do_not_import_sqlite3_or_ship_db_modules():
    web_dir = Path(__file__).resolve().parents[1] / "web"
    offenders: list[str] = []

    for path in sorted(web_dir.glob("*.py")):
        if path.name.endswith("_db.py"):
            offenders.append(path.name)
            continue

        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(alias.name == "sqlite3" for alias in node.names):
                    offenders.append(f"{path.name}: import sqlite3")
            elif isinstance(node, ast.ImportFrom):
                if node.module == "sqlite3":
                    offenders.append(f"{path.name}: from sqlite3 import ...")

    assert offenders == []


def test_runtime_scripts_and_make_targets_do_not_reference_sqlite_artifacts():
    root = Path(__file__).resolve().parents[1]
    checked = [
        root / "Makefile",
        root / "scripts" / "fix_lxc_docker.sh",
    ]
    banned = [
        "sqlite3",
        "tests/test_sequences_db.py",
        "/app/output/jobs.db",
        "/app/output/sequences.db",
    ]
    offenders = []

    for path in checked:
        text = path.read_text(encoding="utf-8")
        for needle in banned:
            if needle in text:
                offenders.append(f"{path.relative_to(root)}: {needle}")

    assert offenders == []
