"""PostgreSQL store for operator-defined OEM hardware profiles.

The built-in profiles live in ``autopilot-proxmox/files/oem_profiles.yml``
and remain read-only at the file level. This module stores any custom
profiles operators create through the API. The merged loader in
``web.oem_profiles_loader`` (callers go through that, not this module
directly) combines the two layers with custom rows winning on key
collision.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from psycopg import Connection

from web import db_pg


SCHEMA = """
CREATE TABLE IF NOT EXISTS oem_profiles_custom (
    key text PRIMARY KEY,
    manufacturer text NOT NULL,
    product text NOT NULL,
    family text NOT NULL,
    sku text NOT NULL,
    chassis_type smallint NOT NULL,
    serial_prefix text NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    created_by text NULL,
    updated_by text NULL
);
"""

DROP_SCHEMA_FOR_TESTS = """
DROP TABLE IF EXISTS oem_profiles_custom CASCADE;
"""

VALID_CHASSIS_TYPES = frozenset({3, 8, 9, 10, 14, 15, 30, 31, 32, 35})

KEY_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class OemProfileValidationError(ValueError):
    """Raised when a profile payload fails validation."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _row(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    out["created_at"] = _iso(out.get("created_at"))
    out["updated_at"] = _iso(out.get("updated_at"))
    return out


def init(conn: Connection | None = None) -> None:
    own = conn is None
    conn = conn or db_pg.connect()
    try:
        conn.execute(SCHEMA)
        conn.commit()
    finally:
        if own:
            conn.close()


def reset_for_tests(conn: Connection) -> None:
    conn.execute(DROP_SCHEMA_FOR_TESTS)
    conn.commit()


def _validate_key(key: str) -> str:
    cleaned = (key or "").strip().lower()
    if not cleaned or len(cleaned) > 64 or not KEY_RE.match(cleaned):
        raise OemProfileValidationError(
            "key must be lowercase kebab-case, 1-64 chars, [a-z0-9-]+"
        )
    return cleaned


def _validate_text(value: Any, *, field: str, limit: int = 240) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise OemProfileValidationError(f"{field} is required")
    if len(cleaned) > limit:
        raise OemProfileValidationError(f"{field} must be {limit} chars or fewer")
    return cleaned


def _validate_chassis_type(value: Any) -> int:
    try:
        as_int = int(value)
    except (TypeError, ValueError):
        raise OemProfileValidationError(
            "chassis_type must be one of: 3, 8, 9, 10, 14, 15, 30, 31, 32, 35"
        )
    if as_int not in VALID_CHASSIS_TYPES:
        raise OemProfileValidationError(
            "chassis_type must be one of: 3, 8, 9, 10, 14, 15, 30, 31, 32, 35"
        )
    return as_int


def _validate_serial_prefix(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    if len(cleaned) > 32:
        raise OemProfileValidationError("serial_prefix must be 32 chars or fewer")
    return cleaned


def validate_profile_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized profile dict with cleaned values."""
    return {
        "manufacturer": _validate_text(payload.get("manufacturer"), field="manufacturer"),
        "product": _validate_text(payload.get("product"), field="product"),
        "family": _validate_text(payload.get("family"), field="family"),
        "sku": _validate_text(payload.get("sku"), field="sku"),
        "chassis_type": _validate_chassis_type(payload.get("chassis_type")),
        "serial_prefix": _validate_serial_prefix(payload.get("serial_prefix")),
    }


def list_profiles(conn: Connection) -> list[dict]:
    init(conn)
    rows = conn.execute(
        """
        SELECT *
        FROM oem_profiles_custom
        ORDER BY key ASC
        """
    ).fetchall()
    return [_row(row) for row in rows]


def get_profile(conn: Connection, key: str) -> dict | None:
    init(conn)
    cleaned = _validate_key(key)
    row = conn.execute(
        "SELECT * FROM oem_profiles_custom WHERE key = %s",
        (cleaned,),
    ).fetchone()
    return _row(row)


def create_profile(
    conn: Connection,
    *,
    key: str,
    payload: dict[str, Any],
    created_by: str | None = None,
    commit: bool = True,
) -> dict:
    init(conn)
    cleaned_key = _validate_key(key)
    fields = validate_profile_fields(payload)
    now = _now()
    by = (created_by or "").strip() or None
    existing = conn.execute(
        "SELECT 1 FROM oem_profiles_custom WHERE key = %s",
        (cleaned_key,),
    ).fetchone()
    if existing is not None:
        raise OemProfileValidationError("custom profile with this key already exists")
    row = conn.execute(
        """
        INSERT INTO oem_profiles_custom (
            key, manufacturer, product, family, sku, chassis_type, serial_prefix,
            created_at, updated_at, created_by, updated_by
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            cleaned_key,
            fields["manufacturer"],
            fields["product"],
            fields["family"],
            fields["sku"],
            fields["chassis_type"],
            fields["serial_prefix"],
            now,
            now,
            by,
            by,
        ),
    ).fetchone()
    if commit:
        conn.commit()
    return _row(dict(row))


def update_profile(
    conn: Connection,
    key: str,
    *,
    payload: dict[str, Any],
    updated_by: str | None = None,
    commit: bool = True,
) -> dict | None:
    init(conn)
    cleaned_key = _validate_key(key)
    fields = validate_profile_fields(payload)
    now = _now()
    by = (updated_by or "").strip() or None
    row = conn.execute(
        """
        UPDATE oem_profiles_custom
        SET manufacturer = %s,
            product = %s,
            family = %s,
            sku = %s,
            chassis_type = %s,
            serial_prefix = %s,
            updated_at = %s,
            updated_by = %s
        WHERE key = %s
        RETURNING *
        """,
        (
            fields["manufacturer"],
            fields["product"],
            fields["family"],
            fields["sku"],
            fields["chassis_type"],
            fields["serial_prefix"],
            now,
            by,
            cleaned_key,
        ),
    ).fetchone()
    if commit:
        conn.commit()
    return _row(dict(row)) if row else None


def delete_profile(conn: Connection, key: str, *, commit: bool = True) -> bool:
    init(conn)
    cleaned_key = _validate_key(key)
    deleted = conn.execute(
        "DELETE FROM oem_profiles_custom WHERE key = %s",
        (cleaned_key,),
    ).rowcount
    if commit:
        conn.commit()
    return bool(deleted)
