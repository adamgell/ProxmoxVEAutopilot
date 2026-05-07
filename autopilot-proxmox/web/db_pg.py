"""Shared PostgreSQL application database helpers."""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row


def database_url() -> str:
    dsn = (
        os.environ.get("AUTOPILOT_DATABASE_URL")
        or os.environ.get("AUTOPILOT_TS_ENGINE_DATABASE_URL")
        or ""
    ).strip()
    if not dsn:
        raise RuntimeError(
            "Postgres database URL is required; set AUTOPILOT_DATABASE_URL "
            "or AUTOPILOT_TS_ENGINE_DATABASE_URL"
        )
    return dsn


def connect(dsn: str | None = None) -> Connection:
    return psycopg.connect(dsn or database_url(), row_factory=dict_row)


@contextmanager
def connection(dsn: str | None = None) -> Iterator[Connection]:
    with connect(dsn) as conn:
        yield conn
