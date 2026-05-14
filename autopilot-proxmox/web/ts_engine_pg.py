"""PostgreSQL repository for Task Sequence Engine v2.

This module is intentionally separate from the existing SQLite
``sequences_db`` module. Engine v2 uses PostgreSQL for ordered task
sequence trees, immutable run plans, step events, and content manifests,
while the current production WinPE path can continue to use the old
SQLite-backed compatibility tables during migration.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Optional

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


SCHEMA_VERSION = 1
CONTENT_TYPES = frozenset({"app", "package", "script", "driver", "os_image"})
REBOOT_BEHAVIORS = frozenset({"none", "optional", "required", "deferred"})
_INIT_LOCK = Lock()
_INIT_DONE = False
_INIT_LOCK_KEY = "proxmoxveautopilot:ts_engine_pg:init"


SCHEMA = """
CREATE TABLE IF NOT EXISTS ts_engine_schema_migrations (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS ts_task_sequences (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    description text,
    enabled boolean NOT NULL DEFAULT true,
    current_version_id uuid NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    created_by text,
    updated_by text
);

CREATE TABLE IF NOT EXISTS ts_task_sequence_nodes (
    id uuid PRIMARY KEY,
    sequence_id uuid NOT NULL REFERENCES ts_task_sequences(id) ON DELETE CASCADE,
    parent_id uuid NULL REFERENCES ts_task_sequence_nodes(id) ON DELETE CASCADE,
    position integer NOT NULL,
    node_type text NOT NULL CHECK (node_type IN ('group', 'step')),
    name text NOT NULL,
    description text,
    kind text NULL,
    phase text NOT NULL DEFAULT 'any',
    enabled boolean NOT NULL DEFAULT true,
    condition_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    variables_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    params_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    content_refs_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    continue_on_error boolean NOT NULL DEFAULT false,
    retry_count integer NOT NULL DEFAULT 0,
    retry_delay_seconds integer NOT NULL DEFAULT 10,
    timeout_seconds integer NULL,
    reboot_behavior text NOT NULL DEFAULT 'none',
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    UNIQUE (sequence_id, parent_id, position),
    CHECK ((node_type = 'group' AND kind IS NULL) OR (node_type = 'step' AND kind IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_ts_nodes_sequence_parent_position
    ON ts_task_sequence_nodes(sequence_id, parent_id, position);

CREATE UNIQUE INDEX IF NOT EXISTS idx_ts_nodes_sequence_root_position_unique
    ON ts_task_sequence_nodes(sequence_id, position)
    WHERE parent_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_ts_nodes_sequence_child_position_unique
    ON ts_task_sequence_nodes(sequence_id, parent_id, position)
    WHERE parent_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS ts_task_sequence_versions (
    id uuid PRIMARY KEY,
    sequence_id uuid NOT NULL REFERENCES ts_task_sequences(id) ON DELETE CASCADE,
    version integer NOT NULL,
    source_hash text NOT NULL,
    compiled_tree_json jsonb NOT NULL,
    compiled_at timestamptz NOT NULL,
    compiled_by text,
    notes text,
    UNIQUE (sequence_id, version),
    UNIQUE (sequence_id, source_hash)
);

CREATE TABLE IF NOT EXISTS ts_provisioning_runs (
    id uuid PRIMARY KEY,
    legacy_run_id integer NULL,
    sequence_id uuid NOT NULL REFERENCES ts_task_sequences(id),
    sequence_version_id uuid NOT NULL REFERENCES ts_task_sequence_versions(id),
    state text NOT NULL,
    phase text NULL,
    cursor_step_id uuid NULL,
    vmid integer NULL,
    vm_uuid text NULL,
    computer_name text NULL,
    serial_number text NULL,
    deployment_target_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    run_variables_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    started_at timestamptz NOT NULL,
    finished_at timestamptz NULL,
    last_error text NULL,
    created_by text
);

CREATE INDEX IF NOT EXISTS idx_ts_runs_state_phase
    ON ts_provisioning_runs(state, phase);

CREATE TABLE IF NOT EXISTS ts_run_plan_steps (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES ts_provisioning_runs(id) ON DELETE CASCADE,
    source_node_id uuid NULL,
    parent_source_node_id uuid NULL,
    ordinal integer NOT NULL,
    depth integer NOT NULL,
    path text NOT NULL,
    name text NOT NULL,
    kind text NOT NULL,
    phase text NOT NULL,
    state text NOT NULL DEFAULT 'pending',
    condition_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    condition_result_json jsonb NULL,
    variables_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    params_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    resolved_params_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    content_refs_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    continue_on_error boolean NOT NULL DEFAULT false,
    retry_count integer NOT NULL DEFAULT 0,
    retry_delay_seconds integer NOT NULL DEFAULT 10,
    timeout_seconds integer NULL,
    reboot_behavior text NOT NULL DEFAULT 'none',
    attempt integer NOT NULL DEFAULT 0,
    claimed_by text NULL,
    claimed_at timestamptz NULL,
    started_at timestamptz NULL,
    finished_at timestamptz NULL,
    last_error text NULL,
    UNIQUE (run_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_ts_run_steps_claim
    ON ts_run_plan_steps(run_id, state, phase, ordinal);

CREATE TABLE IF NOT EXISTS ts_run_step_events (
    id bigserial PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES ts_provisioning_runs(id) ON DELETE CASCADE,
    step_id uuid NULL REFERENCES ts_run_plan_steps(id) ON DELETE CASCADE,
    event_type text NOT NULL,
    severity text NOT NULL DEFAULT 'info',
    agent_id text NULL,
    phase text NULL,
    attempt integer NULL,
    message text NULL,
    data_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS ts_run_step_logs (
    id bigserial PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES ts_provisioning_runs(id) ON DELETE CASCADE,
    step_id uuid NULL REFERENCES ts_run_plan_steps(id) ON DELETE CASCADE,
    agent_id text NULL,
    stream text NOT NULL,
    content text NOT NULL,
    created_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS ts_content_items (
    id uuid PRIMARY KEY,
    name text NOT NULL UNIQUE,
    content_type text NOT NULL CHECK (content_type IN ('app', 'package', 'script', 'driver', 'os_image')),
    description text,
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS ts_content_versions (
    id uuid PRIMARY KEY,
    content_item_id uuid NOT NULL REFERENCES ts_content_items(id) ON DELETE CASCADE,
    version text NOT NULL,
    sha256 text NOT NULL,
    size_bytes bigint NULL,
    source_uri text NOT NULL,
    architecture text NOT NULL DEFAULT 'any',
    target_os text NOT NULL DEFAULT 'any',
    reboot_behavior text NOT NULL DEFAULT 'none' CHECK (reboot_behavior IN ('none', 'optional', 'required', 'deferred')),
    conditions_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL,
    created_by text,
    UNIQUE (content_item_id, version)
);

ALTER TABLE ts_content_versions
    ADD COLUMN IF NOT EXISTS architecture text NOT NULL DEFAULT 'any';
ALTER TABLE ts_content_versions
    ADD COLUMN IF NOT EXISTS target_os text NOT NULL DEFAULT 'any';
ALTER TABLE ts_content_versions
    ADD COLUMN IF NOT EXISTS reboot_behavior text NOT NULL DEFAULT 'none';
ALTER TABLE ts_content_versions
    ADD COLUMN IF NOT EXISTS conditions_json jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_ts_content_versions_sha256
    ON ts_content_versions(sha256);

CREATE TABLE IF NOT EXISTS ts_run_content_manifest (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES ts_provisioning_runs(id) ON DELETE CASCADE,
    content_version_id uuid NOT NULL REFERENCES ts_content_versions(id),
    logical_name text NOT NULL,
    content_type text NOT NULL,
    required_phase text NOT NULL,
    required boolean NOT NULL DEFAULT true,
    source_uri text NOT NULL,
    sha256 text NOT NULL,
    size_bytes bigint NULL,
    staging_path text NULL,
    status text NOT NULL DEFAULT 'pending',
    staging_attempts integer NOT NULL DEFAULT 0,
    staged_by text NULL,
    staged_at timestamptz NULL,
    last_error text NULL,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL,
    UNIQUE (run_id, logical_name)
);

ALTER TABLE ts_run_content_manifest
    ADD COLUMN IF NOT EXISTS staging_attempts integer NOT NULL DEFAULT 0;
ALTER TABLE ts_run_content_manifest
    ADD COLUMN IF NOT EXISTS staged_by text NULL;
ALTER TABLE ts_run_content_manifest
    ADD COLUMN IF NOT EXISTS staged_at timestamptz NULL;
ALTER TABLE ts_run_content_manifest
    ADD COLUMN IF NOT EXISTS last_error text NULL;
"""


DROP_SCHEMA_FOR_TESTS = """
DROP TABLE IF EXISTS ts_run_content_manifest CASCADE;
DROP TABLE IF EXISTS ts_content_versions CASCADE;
DROP TABLE IF EXISTS ts_content_items CASCADE;
DROP TABLE IF EXISTS ts_run_step_logs CASCADE;
DROP TABLE IF EXISTS ts_run_step_events CASCADE;
DROP TABLE IF EXISTS ts_run_plan_steps CASCADE;
DROP TABLE IF EXISTS ts_provisioning_runs CASCADE;
DROP TABLE IF EXISTS ts_task_sequence_versions CASCADE;
DROP TABLE IF EXISTS ts_task_sequence_nodes CASCADE;
DROP TABLE IF EXISTS ts_task_sequences CASCADE;
DROP TABLE IF EXISTS ts_engine_schema_migrations CASCADE;
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


def _json(value: Any) -> Jsonb:
    return Jsonb(value)


def _commit(conn: Connection) -> None:
    conn.commit()


def init(conn: Connection) -> None:
    """Create the v2 engine schema. Safe to call repeatedly."""
    global _INIT_DONE
    if _INIT_DONE:
        return
    with _INIT_LOCK:
        if _INIT_DONE:
            return
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_INIT_LOCK_KEY,))
        conn.execute(SCHEMA)
        conn.execute(
            """
            INSERT INTO ts_engine_schema_migrations (version, applied_at)
            VALUES (%s, %s)
            ON CONFLICT (version) DO NOTHING
            """,
            (SCHEMA_VERSION, _now()),
        )
        _commit(conn)
        _INIT_DONE = True


def reset_for_tests(conn: Connection) -> None:
    """Drop v2 engine tables. Only test code should call this."""
    global _INIT_DONE
    conn.execute(DROP_SCHEMA_FOR_TESTS)
    _commit(conn)
    _INIT_DONE = False


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat(timespec="seconds")
    return str(value)


def create_sequence(
    conn: Connection,
    *,
    name: str,
    description: str = "",
    created_by: Optional[str] = None,
) -> str:
    sequence_id = _new_id()
    now = _now()
    conn.execute(
        """
        INSERT INTO ts_task_sequences
            (id, name, description, created_at, updated_at, created_by, updated_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (sequence_id, name, description, now, now, created_by, created_by),
    )
    _commit(conn)
    return sequence_id


def list_sequences(conn: Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            s.*,
            COUNT(n.id) FILTER (WHERE n.node_type = 'step') AS step_count,
            COUNT(n.id) FILTER (WHERE n.node_type = 'group') AS group_count
        FROM ts_task_sequences s
        LEFT JOIN ts_task_sequence_nodes n ON n.sequence_id = s.id
        GROUP BY s.id
        ORDER BY s.updated_at DESC, s.name ASC
        """
    ).fetchall()
    return [
        {
            "id": str(row["id"]),
            "name": row["name"],
            "description": row["description"] or "",
            "enabled": bool(row["enabled"]),
            "current_version_id": (
                str(row["current_version_id"]) if row["current_version_id"] else None
            ),
            "step_count": int(row["step_count"]),
            "group_count": int(row["group_count"]),
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
        }
        for row in rows
    ]


def list_sequence_steps(conn: Connection, sequence_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT *
        FROM ts_task_sequence_nodes
        WHERE sequence_id = %s AND node_type = 'step'
        ORDER BY parent_id NULLS FIRST, position, name
        """,
        (sequence_id,),
    ).fetchall()
    return [
        {
            "id": str(row["id"]),
            "sequence_id": str(row["sequence_id"]),
            "parent_id": str(row["parent_id"]) if row["parent_id"] else None,
            "position": int(row["position"]),
            "name": row["name"],
            "kind": row["kind"],
            "phase": row["phase"],
            "enabled": bool(row["enabled"]),
            "content_refs": row["content_refs_json"] or [],
            "retry_count": int(row["retry_count"]),
            "reboot_behavior": row["reboot_behavior"],
            "continue_on_error": bool(row["continue_on_error"]),
        }
        for row in rows
    ]


def add_group(
    conn: Connection,
    *,
    sequence_id: str,
    name: str,
    position: int,
    parent_id: Optional[str] = None,
    enabled: bool = True,
    condition: Optional[dict] = None,
    variables: Optional[dict] = None,
) -> str:
    return _add_node(
        conn,
        sequence_id=sequence_id,
        parent_id=parent_id,
        position=position,
        node_type="group",
        name=name,
        kind=None,
        phase="any",
        enabled=enabled,
        condition=condition or {},
        variables=variables or {},
        params={},
        content_refs=[],
    )


def add_step(
    conn: Connection,
    *,
    sequence_id: str,
    parent_id: Optional[str],
    name: str,
    kind: str,
    phase: str,
    position: int,
    enabled: bool = True,
    condition: Optional[dict] = None,
    variables: Optional[dict] = None,
    params: Optional[dict] = None,
    content_refs: Optional[list[str]] = None,
    continue_on_error: bool = False,
    retry_count: int = 0,
    retry_delay_seconds: int = 10,
    timeout_seconds: Optional[int] = None,
    reboot_behavior: str = "none",
) -> str:
    return _add_node(
        conn,
        sequence_id=sequence_id,
        parent_id=parent_id,
        position=position,
        node_type="step",
        name=name,
        kind=kind,
        phase=phase,
        enabled=enabled,
        condition=condition or {},
        variables=variables or {},
        params=params or {},
        content_refs=content_refs or [],
        continue_on_error=continue_on_error,
        retry_count=retry_count,
        retry_delay_seconds=retry_delay_seconds,
        timeout_seconds=timeout_seconds,
        reboot_behavior=reboot_behavior,
    )


def _add_node(
    conn: Connection,
    *,
    sequence_id: str,
    parent_id: Optional[str],
    position: int,
    node_type: str,
    name: str,
    kind: Optional[str],
    phase: str,
    enabled: bool,
    condition: dict,
    variables: dict,
    params: dict,
    content_refs: list[str],
    continue_on_error: bool = False,
    retry_count: int = 0,
    retry_delay_seconds: int = 10,
    timeout_seconds: Optional[int] = None,
    reboot_behavior: str = "none",
) -> str:
    node_id = _new_id()
    now = _now()
    conn.execute(
        """
        INSERT INTO ts_task_sequence_nodes (
            id, sequence_id, parent_id, position, node_type, name, kind, phase,
            enabled, condition_json, variables_json, params_json,
            content_refs_json, continue_on_error, retry_count,
            retry_delay_seconds, timeout_seconds, reboot_behavior,
            created_at, updated_at
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s
        )
        """,
        (
            node_id,
            sequence_id,
            parent_id,
            position,
            node_type,
            name,
            kind,
            phase,
            enabled,
            _json(condition),
            _json(variables),
            _json(params),
            _json(content_refs),
            continue_on_error,
            retry_count,
            retry_delay_seconds,
            timeout_seconds,
            reboot_behavior,
            now,
            now,
        ),
    )
    _commit(conn)
    return node_id


def compile_sequence(conn: Connection, sequence_id: str, *, compiled_by: str | None = None) -> str:
    nodes = _load_nodes(conn, sequence_id)
    tree = _compiled_tree(nodes)
    source = json.dumps(tree, sort_keys=True, separators=(",", ":"))
    source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
    existing = conn.execute(
        """
        SELECT id FROM ts_task_sequence_versions
        WHERE sequence_id = %s AND source_hash = %s
        """,
        (sequence_id, source_hash),
    ).fetchone()
    if existing:
        return str(existing["id"])

    row = conn.execute(
        """
        SELECT COALESCE(MAX(version), 0) + 1 AS next_version
        FROM ts_task_sequence_versions
        WHERE sequence_id = %s
        """,
        (sequence_id,),
    ).fetchone()
    version_id = _new_id()
    conn.execute(
        """
        INSERT INTO ts_task_sequence_versions (
            id, sequence_id, version, source_hash, compiled_tree_json,
            compiled_at, compiled_by
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            version_id,
            sequence_id,
            int(row["next_version"]),
            source_hash,
            _json(tree),
            _now(),
            compiled_by,
        ),
    )
    conn.execute(
        "UPDATE ts_task_sequences SET current_version_id = %s, updated_at = %s WHERE id = %s",
        (version_id, _now(), sequence_id),
    )
    _commit(conn)
    return version_id


def _load_nodes(conn: Connection, sequence_id: str) -> list[dict]:
    return conn.execute(
        """
        SELECT *
        FROM ts_task_sequence_nodes
        WHERE sequence_id = %s
        ORDER BY parent_id NULLS FIRST, position, created_at
        """,
        (sequence_id,),
    ).fetchall()


def _compiled_tree(nodes: list[dict]) -> list[dict]:
    return [
        {
            "id": str(row["id"]),
            "parent_id": str(row["parent_id"]) if row["parent_id"] else None,
            "position": row["position"],
            "node_type": row["node_type"],
            "name": row["name"],
            "kind": row["kind"],
            "phase": row["phase"],
            "enabled": row["enabled"],
            "condition": row["condition_json"],
            "variables": row["variables_json"],
            "params": row["params_json"],
            "content_refs": row["content_refs_json"],
            "continue_on_error": row["continue_on_error"],
            "retry_count": row["retry_count"],
            "retry_delay_seconds": row["retry_delay_seconds"],
            "timeout_seconds": row["timeout_seconds"],
            "reboot_behavior": row["reboot_behavior"],
        }
        for row in sorted(
            nodes,
            key=lambda r: (str(r["parent_id"] or ""), r["position"], str(r["id"])),
        )
    ]


def create_run_from_version(
    conn: Connection,
    *,
    sequence_version_id: str,
    deployment_target: Optional[dict] = None,
    run_variables: Optional[dict] = None,
    created_by: Optional[str] = None,
    resolve_content: bool = False,
) -> str:
    version = conn.execute(
        "SELECT * FROM ts_task_sequence_versions WHERE id = %s",
        (sequence_version_id,),
    ).fetchone()
    if not version:
        raise ValueError(f"sequence version not found: {sequence_version_id}")

    target = deployment_target or {}
    variables = run_variables or {}
    run_id = _new_id()
    conn.execute(
        """
        INSERT INTO ts_provisioning_runs (
            id, sequence_id, sequence_version_id, state, phase, vmid, vm_uuid,
            computer_name, serial_number, deployment_target_json,
            run_variables_json, started_at, created_by
        )
        VALUES (%s, %s, %s, 'queued', NULL, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            run_id,
            version["sequence_id"],
            sequence_version_id,
            target.get("vmid"),
            target.get("vm_uuid"),
            target.get("computer_name"),
            target.get("serial_number"),
            _json(target),
            _json(variables),
            _now(),
            created_by,
        ),
    )
    for step in _flatten_steps(version["compiled_tree_json"]):
        condition_result = evaluate_condition(
            step["condition_json"],
            variables=variables,
            target=target,
        )
        state = (
            "pending"
            if step["enabled"] and condition_result["matched"]
            else "skipped"
        )
        conn.execute(
            """
            INSERT INTO ts_run_plan_steps (
                id, run_id, source_node_id, parent_source_node_id, ordinal,
                depth, path, name, kind, phase, state, condition_json,
                condition_result_json, variables_json, params_json, resolved_params_json,
                content_refs_json, continue_on_error, retry_count,
                retry_delay_seconds, timeout_seconds, reboot_behavior
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                _new_id(),
                run_id,
                step["source_node_id"],
                step["parent_source_node_id"],
                step["ordinal"],
                step["depth"],
                step["path"],
                step["name"],
                step["kind"],
                step["phase"],
                state,
                _json(step["condition_json"]),
                _json(condition_result),
                _json(step["variables_json"]),
                _json(step["params_json"]),
                _json(step["resolved_params_json"]),
                _json(step["content_refs_json"]),
                step["continue_on_error"],
                step["retry_count"],
                step["retry_delay_seconds"],
                step["timeout_seconds"],
                step["reboot_behavior"],
            ),
        )
    _append_event(conn, run_id=run_id, event_type="run_created")
    if resolve_content:
        try:
            resolve_run_content_manifest(conn, run_id)
        except Exception:
            conn.rollback()
            raise
    else:
        _commit(conn)
    return run_id


def _flatten_steps(compiled_tree: list[dict]) -> list[dict]:
    children: dict[Optional[str], list[dict]] = {}
    for node in compiled_tree:
        children.setdefault(node["parent_id"], []).append(node)
    for child_list in children.values():
        child_list.sort(key=lambda n: (n["position"], n["name"], n["id"]))

    out: list[dict] = []

    def walk(parent_id: Optional[str], path_parts: list[str], depth: int) -> None:
        for node in children.get(parent_id, []):
            node_path = path_parts + [node["name"]]
            if node["node_type"] == "group":
                walk(node["id"], node_path, depth + 1)
                continue
            out.append(
                {
                    "source_node_id": node["id"],
                    "parent_source_node_id": node["parent_id"],
                    "ordinal": len(out),
                    "depth": depth,
                    "path": " / ".join(node_path),
                    "name": node["name"],
                    "kind": node["kind"],
                    "phase": node["phase"],
                    "enabled": node["enabled"],
                    "condition_json": node["condition"],
                    "variables_json": node["variables"],
                    "params_json": node["params"],
                    "resolved_params_json": node["params"],
                    "content_refs_json": node["content_refs"],
                    "continue_on_error": node["continue_on_error"],
                    "retry_count": node["retry_count"],
                    "retry_delay_seconds": node["retry_delay_seconds"],
                    "timeout_seconds": node["timeout_seconds"],
                    "reboot_behavior": node["reboot_behavior"],
                }
            )

    walk(None, [], 0)
    return out


def evaluate_condition(
    condition: dict,
    *,
    variables: Optional[dict] = None,
    target: Optional[dict] = None,
) -> dict:
    """Evaluate the first-cut task-sequence condition dialect."""
    context = {
        "variables": variables or {},
        "target": target or {},
    }
    matched, reason = _eval_condition(condition or {}, context)
    return {"matched": matched, "reason": reason}


def _eval_condition(condition: dict, context: dict) -> tuple[bool, str]:
    if not condition:
        return True, "empty"
    if "all" in condition:
        results = [_eval_condition(child, context) for child in condition["all"]]
        return all(result[0] for result in results), "all"
    if "any" in condition:
        results = [_eval_condition(child, context) for child in condition["any"]]
        return any(result[0] for result in results), "any"
    if "not" in condition:
        matched, _reason = _eval_condition(condition["not"], context)
        return not matched, "not"
    if "exists" in condition:
        path = condition["exists"]
        return _resolve_path(context, path) is not None, f"exists:{path}"
    if "eq" in condition:
        path, expected = condition["eq"]
        return _resolve_path(context, path) == expected, f"eq:{path}"
    if "ne" in condition:
        path, expected = condition["ne"]
        return _resolve_path(context, path) != expected, f"ne:{path}"
    raise ValueError(f"unsupported condition: {condition}")


def _resolve_path(context: dict, path: str) -> Any:
    current: Any = context
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def list_run_steps(conn: Connection, run_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT *
        FROM ts_run_plan_steps
        WHERE run_id = %s
        ORDER BY ordinal
        """,
        (run_id,),
    ).fetchall()
    return [_normalize_step(row) for row in rows]


def list_runs(conn: Connection, *, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            r.*,
            s.name AS sequence_name,
            v.version AS sequence_version,
            COUNT(DISTINCT st.id) AS step_count,
            COUNT(DISTINCT st.id) FILTER (WHERE st.state = 'done') AS done_count,
            COUNT(DISTINCT st.id) FILTER (WHERE st.state = 'running') AS running_count,
            COUNT(DISTINCT st.id) FILTER (WHERE st.state = 'failed') AS failed_count,
            COUNT(DISTINCT m.id) AS manifest_count
        FROM ts_provisioning_runs r
        JOIN ts_task_sequences s ON s.id = r.sequence_id
        JOIN ts_task_sequence_versions v ON v.id = r.sequence_version_id
        LEFT JOIN ts_run_plan_steps st ON st.run_id = r.id
        LEFT JOIN ts_run_content_manifest m ON m.run_id = r.id
        GROUP BY r.id, s.name, v.version
        ORDER BY r.started_at DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    return [
        {
            "id": str(row["id"]),
            "legacy_run_id": row["legacy_run_id"],
            "sequence_id": str(row["sequence_id"]),
            "sequence_name": row["sequence_name"],
            "sequence_version_id": str(row["sequence_version_id"]),
            "sequence_version": int(row["sequence_version"]),
            "state": row["state"],
            "phase": row["phase"],
            "cursor_step_id": (
                str(row["cursor_step_id"]) if row["cursor_step_id"] else None
            ),
            "vmid": row["vmid"],
            "vm_uuid": row["vm_uuid"],
            "computer_name": row["computer_name"],
            "serial_number": row["serial_number"],
            "started_at": _iso(row["started_at"]),
            "finished_at": _iso(row["finished_at"]),
            "last_error": row["last_error"],
            "step_count": int(row["step_count"]),
            "done_count": int(row["done_count"]),
            "running_count": int(row["running_count"]),
            "failed_count": int(row["failed_count"]),
            "manifest_count": int(row["manifest_count"]),
        }
        for row in rows
    ]


def mark_steps_done_by_kind(
    conn: Connection,
    *,
    run_id: str,
    kinds: list[str] | tuple[str, ...] | set[str],
    agent_id: str = "controller",
    message: Optional[str] = None,
    data: Optional[dict] = None,
) -> int:
    """Mark matching run-plan steps done from external lifecycle evidence.

    CloudOSD owns deployment orchestration in PE/full OS today, while the v2
    engine owns the immutable run plan shown in the UI. This bridge lets a
    trusted controller synchronize plan progress without pretending the OSD v2
    agent claimed each CloudOSD step.
    """
    normalized_kinds = sorted({str(kind) for kind in kinds if str(kind)})
    if not normalized_kinds:
        return 0
    rows = conn.execute(
        """
        SELECT *
        FROM ts_run_plan_steps
        WHERE run_id = %s
          AND kind = ANY(%s)
        ORDER BY ordinal
        """,
        (run_id, normalized_kinds),
    ).fetchall()
    now = _now()
    changed = 0
    for row in rows:
        if row["state"] == "done":
            continue
        conn.execute(
            """
            UPDATE ts_run_plan_steps
            SET state = 'done',
                started_at = COALESCE(started_at, %s),
                finished_at = COALESCE(finished_at, %s),
                claimed_by = COALESCE(claimed_by, %s),
                claimed_at = COALESCE(claimed_at, %s),
                last_error = NULL
            WHERE id = %s
            """,
            (now, now, agent_id, now, row["id"]),
        )
        _append_event(
            conn,
            run_id=run_id,
            step_id=str(row["id"]),
            event_type="step_done",
            severity="info",
            agent_id=agent_id,
            phase=row["phase"],
            attempt=row["attempt"],
            message=message or "step completed from external lifecycle evidence",
            data=data or {},
        )
        changed += 1

    remaining = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM ts_run_plan_steps
        WHERE run_id = %s
          AND state IN ('pending', 'running', 'awaiting_reboot', 'failed')
        """,
        (run_id,),
    ).fetchone()["count"]
    if int(remaining or 0) == 0:
        conn.execute(
            """
            UPDATE ts_provisioning_runs
            SET state = 'done',
                phase = 'full_os',
                finished_at = COALESCE(finished_at, %s),
                cursor_step_id = COALESCE(
                    cursor_step_id,
                    (
                        SELECT id
                        FROM ts_run_plan_steps
                        WHERE run_id = %s
                        ORDER BY ordinal DESC
                        LIMIT 1
                    )
                )
            WHERE id = %s
              AND state <> 'failed'
            """,
            (now, run_id, run_id),
        )
    elif changed:
        last_phase = rows[-1]["phase"] if rows else None
        conn.execute(
            """
            UPDATE ts_provisioning_runs
            SET phase = COALESCE(%s, phase)
            WHERE id = %s
              AND state NOT IN ('done', 'failed')
            """,
            (last_phase, run_id),
        )
    _commit(conn)
    return changed


def list_recent_manifest_items(conn: Connection, *, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            m.*,
            r.state AS run_state,
            s.name AS sequence_name
        FROM ts_run_content_manifest m
        JOIN ts_provisioning_runs r ON r.id = m.run_id
        JOIN ts_task_sequences s ON s.id = r.sequence_id
        ORDER BY m.created_at DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    return [
        {
            "id": str(row["id"]),
            "run_id": str(row["run_id"]),
            "sequence_name": row["sequence_name"],
            "run_state": row["run_state"],
            "logical_name": row["logical_name"],
            "content_type": row["content_type"],
            "required_phase": row["required_phase"],
            "required": bool(row["required"]),
            "source_uri": row["source_uri"],
            "sha256": row["sha256"],
            "size_bytes": row["size_bytes"],
            "staging_path": row["staging_path"],
            "status": row["status"],
            "staging_attempts": int(row["staging_attempts"]),
            "last_error": row["last_error"],
            "created_at": _iso(row["created_at"]),
        }
        for row in rows
    ]


def _normalize_step(row: dict) -> dict:
    return {
        **row,
        "id": str(row["id"]),
        "run_id": str(row["run_id"]),
        "source_node_id": str(row["source_node_id"]) if row["source_node_id"] else None,
        "parent_source_node_id": (
            str(row["parent_source_node_id"]) if row["parent_source_node_id"] else None
        ),
    }


def create_content_item(
    conn: Connection,
    *,
    name: str,
    content_type: str,
    description: str = "",
) -> str:
    if content_type not in CONTENT_TYPES:
        allowed = ", ".join(sorted(CONTENT_TYPES))
        raise ValueError(f"content_type must be one of: {allowed}")
    item_id = _new_id()
    now = _now()
    conn.execute(
        """
        INSERT INTO ts_content_items
            (id, name, content_type, description, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (item_id, name, content_type, description, now, now),
    )
    _commit(conn)
    return item_id


def create_content_version(
    conn: Connection,
    *,
    content_item_id: str,
    version: str,
    sha256: str,
    source_uri: str,
    size_bytes: Optional[int] = None,
    architecture: str = "any",
    target_os: str = "any",
    reboot_behavior: str = "none",
    conditions: Optional[dict] = None,
    metadata: Optional[dict] = None,
    created_by: Optional[str] = None,
) -> str:
    if reboot_behavior not in REBOOT_BEHAVIORS:
        allowed = ", ".join(sorted(REBOOT_BEHAVIORS))
        raise ValueError(f"reboot_behavior must be one of: {allowed}")
    version_id = _new_id()
    conn.execute(
        """
        INSERT INTO ts_content_versions (
            id, content_item_id, version, sha256, size_bytes, source_uri,
            architecture, target_os, reboot_behavior, conditions_json,
            metadata_json, created_at, created_by
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            version_id,
            content_item_id,
            version,
            sha256.lower(),
            size_bytes,
            source_uri,
            architecture,
            target_os,
            reboot_behavior,
            _json(conditions or {}),
            _json(metadata or {}),
            _now(),
            created_by,
        ),
    )
    _commit(conn)
    return version_id


def list_content_items(conn: Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            ci.id,
            ci.name,
            ci.content_type,
            ci.description,
            ci.enabled,
            latest.id AS latest_version_id,
            latest.version AS latest_version,
            latest.sha256 AS latest_sha256,
            latest.size_bytes AS latest_size_bytes,
            latest.source_uri AS latest_source_uri,
            latest.metadata_json AS latest_metadata
        FROM ts_content_items ci
        LEFT JOIN LATERAL (
            SELECT *
            FROM ts_content_versions cv
            WHERE cv.content_item_id = ci.id
            ORDER BY cv.created_at DESC, cv.id DESC
            LIMIT 1
        ) latest ON true
        ORDER BY ci.name
        """
    ).fetchall()
    return [_normalize_content_item(row) for row in rows]


def get_content_item(conn: Connection, item_id: str) -> dict:
    row = conn.execute(
        """
        SELECT
            ci.id,
            ci.name,
            ci.content_type,
            ci.description,
            ci.enabled,
            latest.id AS latest_version_id,
            latest.version AS latest_version,
            latest.sha256 AS latest_sha256,
            latest.size_bytes AS latest_size_bytes,
            latest.source_uri AS latest_source_uri,
            latest.metadata_json AS latest_metadata
        FROM ts_content_items ci
        LEFT JOIN LATERAL (
            SELECT *
            FROM ts_content_versions cv
            WHERE cv.content_item_id = ci.id
            ORDER BY cv.created_at DESC, cv.id DESC
            LIMIT 1
        ) latest ON true
        WHERE ci.id = %s
        """,
        (item_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"content item not found: {item_id}")
    return _normalize_content_item(row)


def get_content_version(conn: Connection, version_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM ts_content_versions WHERE id = %s",
        (version_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"content version not found: {version_id}")
    return _normalize_content_version(row)


def _normalize_content_item(row: dict) -> dict:
    latest_version = None
    if row["latest_version_id"]:
        latest_version = {
            "id": str(row["latest_version_id"]),
            "version": row["latest_version"],
            "sha256": row["latest_sha256"],
            "size_bytes": row["latest_size_bytes"],
            "source_uri": row["latest_source_uri"],
            "metadata": row["latest_metadata"],
        }
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "content_type": row["content_type"],
        "description": row["description"],
        "enabled": row["enabled"],
        "latest_version": latest_version,
    }


def _normalize_content_version(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "content_item_id": str(row["content_item_id"]),
        "version": row["version"],
        "sha256": row["sha256"],
        "size_bytes": row["size_bytes"],
        "source_uri": row["source_uri"],
        "architecture": row["architecture"],
        "target_os": row["target_os"],
        "reboot_behavior": row["reboot_behavior"],
        "conditions": row["conditions_json"],
        "metadata": row["metadata_json"],
    }


def build_content_manifest_v1(conn: Connection) -> dict:
    """Build the global Content Manifest v1 from latest enabled versions."""
    from web.content_manifest import manifest_digest, validate_manifest

    rows = conn.execute(
        """
        SELECT
            ci.name,
            ci.content_type,
            latest.version,
            latest.sha256,
            latest.size_bytes,
            latest.source_uri,
            latest.architecture,
            latest.target_os,
            latest.reboot_behavior,
            latest.conditions_json,
            latest.metadata_json
        FROM ts_content_items ci
        JOIN LATERAL (
            SELECT *
            FROM ts_content_versions cv
            WHERE cv.content_item_id = ci.id
            ORDER BY cv.created_at DESC, cv.id DESC
            LIMIT 1
        ) latest ON true
        WHERE ci.enabled = true
        ORDER BY ci.name
        """
    ).fetchall()
    raw = {
        "schema_version": 1,
        "items": [
            {
                "id": row["name"],
                "kind": row["content_type"],
                "name": row["name"],
                "version": row["version"],
                "source_uri": row["source_uri"],
                "sha256": row["sha256"],
                "size_bytes": int(row["size_bytes"] or 0),
                "architecture": row["architecture"],
                "target_os": row["target_os"],
                "reboot_behavior": row["reboot_behavior"],
                "conditions": row["conditions_json"] or {},
                "metadata": row["metadata_json"] or {},
            }
            for row in rows
        ],
    }
    manifest = validate_manifest(raw).to_dict()
    return {
        "schema_version": 1,
        "manifest": manifest,
        "digest": manifest_digest(manifest),
        "item_count": len(manifest["items"]),
    }


def add_manifest_item(
    conn: Connection,
    *,
    run_id: str,
    content_version_id: str,
    logical_name: str,
    required_phase: str,
    staging_path: Optional[str] = None,
    required: bool = True,
    metadata: Optional[dict] = None,
) -> str:
    row = conn.execute(
        """
        SELECT cv.*, ci.content_type
        FROM ts_content_versions cv
        JOIN ts_content_items ci ON ci.id = cv.content_item_id
        WHERE cv.id = %s
        """,
        (content_version_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"content version not found: {content_version_id}")
    merged_metadata = {
        **(row["metadata_json"] or {}),
        **(metadata or {}),
    }
    manifest_id = _new_id()
    conn.execute(
        """
        INSERT INTO ts_run_content_manifest (
            id, run_id, content_version_id, logical_name, content_type,
            required_phase, required, source_uri, sha256, size_bytes,
            staging_path, metadata_json, created_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            manifest_id,
            run_id,
            content_version_id,
            logical_name,
            row["content_type"],
            required_phase,
            required,
            row["source_uri"],
            row["sha256"],
            row["size_bytes"],
            staging_path,
            _json(merged_metadata),
            _now(),
        ),
    )
    _commit(conn)
    return manifest_id


def get_manifest_item(conn: Connection, manifest_id: str) -> dict:
    row = conn.execute(
        """
        SELECT
            m.id,
            m.run_id,
            m.logical_name,
            m.content_type,
            cv.version,
            m.sha256,
            m.source_uri,
            m.required_phase,
            m.staging_path,
            m.status,
            m.metadata_json
        FROM ts_run_content_manifest m
        JOIN ts_content_versions cv ON cv.id = m.content_version_id
        WHERE m.id = %s
        """,
        (manifest_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"manifest item not found: {manifest_id}")
    item = {
        "id": str(row["id"]),
        "run_id": str(row["run_id"]),
        "logical_name": row["logical_name"],
        "content_type": row["content_type"],
        "version": row["version"],
        "sha256": row["sha256"],
        "source_uri": row["source_uri"],
        "required_phase": row["required_phase"],
        "staging_path": row["staging_path"],
        "status": row["status"],
    }
    if row["metadata_json"]:
        item["metadata"] = row["metadata_json"]
    return item


def list_run_manifest(conn: Connection, run_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            m.id,
            m.logical_name,
            m.content_type,
            cv.version,
            m.sha256,
            m.source_uri,
            m.required_phase,
            m.staging_path,
            m.status,
            m.metadata_json
        FROM ts_run_content_manifest m
        JOIN ts_content_versions cv ON cv.id = m.content_version_id
        WHERE m.run_id = %s
        ORDER BY m.created_at, m.logical_name
        """,
        (run_id,),
    ).fetchall()
    items = []
    for row in rows:
        item = {
            "logical_name": row["logical_name"],
            "content_type": row["content_type"],
            "version": row["version"],
            "sha256": row["sha256"],
            "source_uri": row["source_uri"],
            "required_phase": row["required_phase"],
            "staging_path": row["staging_path"],
            "status": row["status"],
        }
        if row["metadata_json"]:
            item["metadata"] = row["metadata_json"]
        items.append(item)
    return items


def mark_manifest_item_staging(
    conn: Connection,
    *,
    manifest_id: str,
    run_id: str,
    status: str,
    agent_id: str,
    staging_path: Optional[str] = None,
    error: Optional[str] = None,
) -> dict:
    if status not in {"pending", "staging", "staged", "failed"}:
        raise ValueError(f"unsupported content staging status: {status}")
    now = _now()
    row = conn.execute(
        """
        UPDATE ts_run_content_manifest
        SET status = %s,
            staging_path = COALESCE(%s, staging_path),
            staging_attempts = (
                staging_attempts + CASE WHEN %s = 'staging' THEN 1 ELSE 0 END
            ),
            staged_by = %s,
            staged_at = CASE WHEN %s = 'staged' THEN %s ELSE staged_at END,
            last_error = CASE WHEN %s = 'failed' THEN %s ELSE NULL END
        WHERE id = %s
          AND run_id = %s
        RETURNING
            id,
            run_id,
            logical_name,
            status,
            staging_path,
            staging_attempts,
            staged_by,
            staged_at,
            last_error
        """,
        (
            status,
            staging_path,
            status,
            agent_id,
            status,
            now,
            status,
            error,
            manifest_id,
            run_id,
        ),
    ).fetchone()
    if not row:
        raise ValueError(f"content manifest item not found: {manifest_id}")
    _commit(conn)
    return _normalize_manifest_staging(row)


def list_run_content_staging(conn: Connection, run_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            m.id,
            m.run_id,
            m.logical_name,
            m.content_type,
            cv.version,
            m.status,
            m.staging_path,
            m.staging_attempts,
            m.staged_by,
            m.staged_at,
            m.last_error
        FROM ts_run_content_manifest m
        JOIN ts_content_versions cv ON cv.id = m.content_version_id
        WHERE m.run_id = %s
        ORDER BY m.created_at, m.logical_name
        """,
        (run_id,),
    ).fetchall()
    return [
        {
            **{
                key: value
                for key, value in _normalize_manifest_staging(row).items()
                if key != "run_id"
            },
            "content_type": row["content_type"],
            "version": row["version"],
        }
        for row in rows
    ]


def _normalize_manifest_staging(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "run_id": str(row["run_id"]),
        "logical_name": row["logical_name"],
        "status": row["status"],
        "staging_path": row["staging_path"],
        "staging_attempts": row["staging_attempts"],
        "staged_by": row["staged_by"],
        "staged_at": row["staged_at"].isoformat() if row["staged_at"] else None,
        "last_error": row["last_error"],
    }


def resolve_run_content_manifest(
    conn: Connection,
    run_id: str,
    *,
    staging_roots: Optional[dict[str, str]] = None,
) -> list[dict]:
    """Pin latest content versions referenced by the immutable run plan."""
    roots = {
        "winpe": "X:\\autopilot\\content",
        "full_os": "C:\\ProgramData\\ProxmoxVEAutopilot\\Content",
        "any": "C:\\ProgramData\\ProxmoxVEAutopilot\\Content",
        **(staging_roots or {}),
    }
    steps = conn.execute(
        """
        SELECT phase, content_refs_json
        FROM ts_run_plan_steps
        WHERE run_id = %s
        ORDER BY ordinal
        """,
        (run_id,),
    ).fetchall()
    refs: dict[str, str] = {}
    for step in steps:
        phase = step["phase"]
        for logical_name in step["content_refs_json"]:
            refs.setdefault(logical_name, phase)

    for logical_name, phase in refs.items():
        row = conn.execute(
            """
            SELECT
                cv.id AS content_version_id,
                cv.version,
                cv.sha256,
                cv.size_bytes,
                cv.source_uri,
                cv.metadata_json,
                ci.content_type
            FROM ts_content_items ci
            JOIN ts_content_versions cv ON cv.content_item_id = ci.id
            WHERE ci.name = %s
              AND ci.enabled = true
            ORDER BY cv.created_at DESC, cv.id DESC
            LIMIT 1
            """,
            (logical_name,),
        ).fetchone()
        if not row:
            raise ValueError(f"content reference has no version: {logical_name}")
        root = roots.get(phase, roots["any"])
        staging_path = f"{root}\\{logical_name}\\{row['version']}"
        conn.execute(
            """
            INSERT INTO ts_run_content_manifest (
                id, run_id, content_version_id, logical_name, content_type,
                required_phase, required, source_uri, sha256, size_bytes,
                staging_path, metadata_json, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, true, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id, logical_name) DO NOTHING
            """,
            (
                _new_id(),
                run_id,
                row["content_version_id"],
                logical_name,
                row["content_type"],
                phase,
                row["source_uri"],
                row["sha256"],
                row["size_bytes"],
                staging_path,
                _json(row["metadata_json"] or {}),
                _now(),
            ),
        )
    _commit(conn)
    return list_run_manifest(conn, run_id)


def content_for_step(conn: Connection, step_id: str) -> list[dict]:
    step = get_step(conn, step_id)
    refs = step["content_refs_json"] or []
    if not refs:
        return []
    rows = conn.execute(
        """
        SELECT
            m.id,
            m.logical_name,
            m.content_type,
            cv.version,
            m.sha256,
            m.source_uri,
            m.required_phase,
            m.staging_path,
            m.status,
            m.metadata_json
        FROM ts_run_content_manifest m
        JOIN ts_content_versions cv ON cv.id = m.content_version_id
        WHERE m.run_id = %s
          AND m.logical_name = ANY(%s)
        ORDER BY m.logical_name
        """,
        (step["run_id"], refs),
    ).fetchall()
    out = []
    for row in rows:
        item = {
            "id": str(row["id"]),
            "logical_name": row["logical_name"],
            "content_type": row["content_type"],
            "version": row["version"],
            "sha256": row["sha256"],
            "source_uri": row["source_uri"],
            "required_phase": row["required_phase"],
            "staging_path": row["staging_path"],
            "status": row["status"],
        }
        if row["metadata_json"]:
            item["metadata"] = row["metadata_json"]
        out.append(item)
    return out


def claim_next_step(
    conn: Connection,
    *,
    run_id: str,
    phase: str,
    agent_id: str,
) -> Optional[dict]:
    row = conn.execute(
        """
        SELECT *
        FROM ts_run_plan_steps
        WHERE run_id = %s
          AND state = 'pending'
          AND phase IN (%s, 'any')
        ORDER BY ordinal
        FOR UPDATE SKIP LOCKED
        LIMIT 1
        """,
        (run_id, phase),
    ).fetchone()
    if not row:
        _commit(conn)
        return None

    attempt = int(row["attempt"]) + 1
    now = _now()
    conn.execute(
        """
        UPDATE ts_run_plan_steps
        SET state = 'running',
            attempt = %s,
            claimed_by = %s,
            claimed_at = %s,
            started_at = COALESCE(started_at, %s)
        WHERE id = %s
        """,
        (attempt, agent_id, now, now, row["id"]),
    )
    conn.execute(
        """
        UPDATE ts_provisioning_runs
        SET state = %s, phase = %s, cursor_step_id = %s
        WHERE id = %s
        """,
        (f"running_{phase}", phase, row["id"], run_id),
    )
    _append_event(
        conn,
        run_id=run_id,
        step_id=str(row["id"]),
        event_type="step_claimed",
        agent_id=agent_id,
        phase=phase,
        attempt=attempt,
    )
    _commit(conn)
    claimed = conn.execute(
        "SELECT * FROM ts_run_plan_steps WHERE id = %s",
        (row["id"],),
    ).fetchone()
    normalized = _normalize_step(claimed)
    normalized["content"] = content_for_step(conn, normalized["id"])
    return normalized


def get_step(conn: Connection, step_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM ts_run_plan_steps WHERE id = %s",
        (step_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"step not found: {step_id}")
    return _normalize_step(row)


def get_run(conn: Connection, run_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM ts_provisioning_runs WHERE id = %s",
        (run_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"run not found: {run_id}")
    return {
        **row,
        "id": str(row["id"]),
        "sequence_id": str(row["sequence_id"]),
        "sequence_version_id": str(row["sequence_version_id"]),
        "cursor_step_id": str(row["cursor_step_id"]) if row["cursor_step_id"] else None,
    }


def requeue_step(
    conn: Connection,
    *,
    run_id: str,
    step_id: str,
    requested_by: str = "operator",
    message: str | None = None,
) -> dict:
    step = conn.execute(
        """
        SELECT *
        FROM ts_run_plan_steps
        WHERE id = %s
          AND run_id = %s
        FOR UPDATE
        """,
        (step_id, run_id),
    ).fetchone()
    if not step:
        raise ValueError(f"step not found for run: {step_id}")
    if step["state"] != "failed":
        raise ValueError(f"only failed steps can be requeued: {step_id}")

    conn.execute(
        """
        UPDATE ts_run_plan_steps
        SET state = 'pending',
            attempt = 0,
            claimed_by = NULL,
            claimed_at = NULL,
            started_at = NULL,
            finished_at = NULL,
            last_error = NULL
        WHERE id = %s
        """,
        (step_id,),
    )
    conn.execute(
        """
        UPDATE ts_provisioning_runs
        SET state = %s,
            phase = %s,
            cursor_step_id = NULL,
            finished_at = NULL,
            last_error = NULL
        WHERE id = %s
        """,
        (f"running_{step['phase']}", step["phase"], run_id),
    )
    _append_event(
        conn,
        run_id=run_id,
        step_id=step_id,
        event_type="step_requeued",
        severity="warning",
        agent_id=requested_by,
        phase=step["phase"],
        attempt=0,
        message=message or f"Operator requeued failed step {step['kind']}",
        data={"previous_attempt": step["attempt"], "kind": step["kind"]},
    )
    _commit(conn)
    updated = conn.execute(
        "SELECT * FROM ts_run_plan_steps WHERE id = %s",
        (step_id,),
    ).fetchone()
    return _normalize_step(updated)


def append_step_log(
    conn: Connection,
    *,
    run_id: str,
    step_id: str,
    agent_id: str,
    stream: str,
    content: str,
) -> None:
    conn.execute(
        """
        INSERT INTO ts_run_step_logs (
            run_id, step_id, agent_id, stream, content, created_at
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (run_id, step_id, agent_id, stream, content, _now()),
    )
    _commit(conn)


def complete_step(
    conn: Connection,
    *,
    run_id: str,
    step_id: str,
    agent_id: str,
    status: str,
    message: Optional[str] = None,
    data: Optional[dict] = None,
) -> dict:
    step = conn.execute(
        "SELECT * FROM ts_run_plan_steps WHERE id = %s AND run_id = %s",
        (step_id, run_id),
    ).fetchone()
    if not step:
        raise ValueError(f"step not found for run: {step_id}")

    now = _now()
    if status == "success" and step["reboot_behavior"] == "required":
        step_state = "awaiting_reboot"
        run_state = "awaiting_reboot"
        finished_at = None
        severity = "info"
    elif status == "success":
        step_state = "done"
        run_state = _next_run_state(conn, run_id, excluding_step_id=step_id)
        finished_at = now
        severity = "info"
    elif status == "skipped":
        step_state = "skipped"
        run_state = _next_run_state(conn, run_id, excluding_step_id=step_id)
        finished_at = now
        severity = "info"
    elif status == "reboot_required":
        step_state = "awaiting_reboot"
        run_state = "awaiting_reboot"
        finished_at = None
        severity = "info"
    elif status == "failed":
        retries_remaining = int(step["attempt"]) <= int(step["retry_count"])
        if retries_remaining:
            step_state = "pending"
            run_state = f"running_{step['phase']}"
            finished_at = None
            severity = "warning"
        else:
            step_state = "failed"
            run_state = (
                _next_run_state(conn, run_id, excluding_step_id=step_id)
                if step["continue_on_error"]
                else "failed"
            )
            finished_at = now
            severity = "error"
    else:
        raise ValueError(f"unsupported step status: {status}")

    conn.execute(
        """
        UPDATE ts_run_plan_steps
        SET state = %s,
            finished_at = %s,
            claimed_by = CASE WHEN %s = 'pending' THEN NULL ELSE claimed_by END,
            claimed_at = CASE WHEN %s = 'pending' THEN NULL ELSE claimed_at END,
            last_error = CASE WHEN %s = 'failed' THEN %s ELSE NULL END
        WHERE id = %s
        """,
        (step_state, finished_at, step_state, step_state, status, message, step_id),
    )
    conn.execute(
        """
        UPDATE ts_provisioning_runs
        SET state = %s,
            phase = %s,
            cursor_step_id = %s,
            finished_at = CASE WHEN %s IN ('done', 'failed') THEN %s ELSE finished_at END,
            last_error = CASE WHEN %s = 'failed' THEN %s ELSE last_error END
        WHERE id = %s
        """,
        (
            run_state,
            step["phase"],
            step_id,
            run_state,
            now,
            run_state,
            message,
            run_id,
        ),
    )
    _append_event(
        conn,
        run_id=run_id,
        step_id=step_id,
        event_type=f"step_{step_state}",
        severity=severity,
        agent_id=agent_id,
        phase=step["phase"],
        attempt=step["attempt"],
        message=message,
        data=data or {},
    )
    _commit(conn)
    updated = conn.execute(
        "SELECT * FROM ts_run_plan_steps WHERE id = %s",
        (step_id,),
    ).fetchone()
    return _normalize_step(updated)


def mark_reboot_complete(
    conn: Connection,
    *,
    run_id: str,
    step_id: str,
    agent_id: str,
) -> dict:
    step = conn.execute(
        "SELECT * FROM ts_run_plan_steps WHERE id = %s AND run_id = %s",
        (step_id, run_id),
    ).fetchone()
    if not step:
        raise ValueError(f"step not found for run: {step_id}")
    if step["state"] != "awaiting_reboot":
        raise ValueError(f"step is not awaiting reboot: {step_id}")
    conn.execute(
        "UPDATE ts_run_plan_steps SET state = 'done', finished_at = %s WHERE id = %s",
        (_now(), step_id),
    )
    run_state = _next_run_state(conn, run_id, excluding_step_id=step_id)
    now = _now()
    conn.execute(
        """
        UPDATE ts_provisioning_runs
        SET state = %s,
            phase = %s,
            cursor_step_id = %s,
            finished_at = CASE WHEN %s = 'done' THEN %s ELSE finished_at END
        WHERE id = %s
        """,
        (run_state, step["phase"], step_id, run_state, now, run_id),
    )
    _append_event(
        conn,
        run_id=run_id,
        step_id=step_id,
        event_type="step_reboot_complete",
        agent_id=agent_id,
        phase=step["phase"],
        attempt=step["attempt"],
    )
    _commit(conn)
    updated = conn.execute(
        "SELECT * FROM ts_run_plan_steps WHERE id = %s",
        (step_id,),
    ).fetchone()
    return _normalize_step(updated)


def _next_run_state(
    conn: Connection,
    run_id: str,
    *,
    excluding_step_id: str,
) -> str:
    pending = conn.execute(
        """
        SELECT 1
        FROM ts_run_plan_steps
        WHERE run_id = %s
          AND id <> %s
          AND state IN ('pending', 'running', 'awaiting_reboot')
        LIMIT 1
        """,
        (run_id, excluding_step_id),
    ).fetchone()
    return "queued" if pending else "done"


def _append_event(
    conn: Connection,
    *,
    run_id: str,
    event_type: str,
    step_id: Optional[str] = None,
    severity: str = "info",
    agent_id: Optional[str] = None,
    phase: Optional[str] = None,
    attempt: Optional[int] = None,
    message: Optional[str] = None,
    data: Optional[dict] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO ts_run_step_events (
            run_id, step_id, event_type, severity, agent_id, phase,
            attempt, message, data_json, created_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            run_id,
            step_id,
            event_type,
            severity,
            agent_id,
            phase,
            attempt,
            message,
            _json(data or {}),
            _now(),
        ),
    )


def connect(dsn: str) -> Connection:
    """Return a dict-row psycopg connection for engine callers."""
    from web import db_pg

    return db_pg.connect(dsn)
