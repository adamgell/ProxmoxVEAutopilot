"""Web package compatibility aliases.

The runtime stores are PostgreSQL-backed. These aliases keep older tests and
internal imports working while preventing legacy SQLite modules from shipping.
"""
from __future__ import annotations

from web import device_history_pg as device_history_db
from web import devices_pg as devices_db
from web import jobs_pg as jobs_db
from web import sequences_pg as sequences_db
from web import service_health_pg as service_health

__all__ = [
    "device_history_db",
    "devices_db",
    "jobs_db",
    "sequences_db",
    "service_health",
]
