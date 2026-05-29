"""Onboarding state persistence (Postgres-backed).

NOTE: stub created 2026-05-27 because `web/__init__.py` was modified to
import this module before the implementation was checked in, blocking
all tests behind conftest's autouse `app_database_startup_bootstrap`
fixture (which imports `web.app` -> `web/__init__.py`).

Replace this stub with the real onboarding state implementation. The
expected surface is documented by `tests/test_onboarding_pg.py`:

  - init(conn) -> None       (idempotent; creates `onboarding_state` table)
  - reset_for_tests(conn)    (test-only TRUNCATE)
  - everything else the test file exercises.
"""
from __future__ import annotations

from typing import Any


def init(conn: Any) -> None:
    """Create the onboarding_state table if it does not exist.

    Stub: no-op. The real implementation should be idempotent.
    """
    return None


def reset_for_tests(conn: Any) -> None:
    """Truncate onboarding state for test isolation.

    Stub: no-op.
    """
    return None
