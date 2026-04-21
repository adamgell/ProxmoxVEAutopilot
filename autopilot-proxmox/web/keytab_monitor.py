"""Keytab health probe + self-refresher.

Runs inside the autopilot container; not on the DC. Two entry points:

* :func:`probe_keytab` — checked every monitoring sweep (15 min).
  stat + klist + kinit -k + LDAP kvno compare.

* :func:`refresh_keytab_if_needed` — runs daily (or on demand when
  probe_keytab says the keytab is missing/broken). Uses the AD
  credential from ``monitoring_settings.ad_credential_id`` to kinit,
  then invokes the shared keytab generator from
  ``scripts/ad/refresh_keytab.py``.

Both functions are designed to be called from the existing
:func:`web.app._device_monitor_loop`. They never raise — everything
is captured to ``keytab_health.last_probe_*`` / ``last_refresh_*``
and surfaced via the UI.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from web import device_history_db

log = logging.getLogger(__name__)


# Probe status vocabulary. Keep in sync with monitoring_view classifier
# and the settings-page panel so the UI badges line up with the raw
# value stored in the DB.
STATUS_OK = "ok"
STATUS_STALE = "stale"         # mtime > yellow threshold
STATUS_MISSING = "missing"     # keytab file not present
STATUS_BROKEN = "broken"       # file exists but kinit/klist fails
STATUS_KVNO_MISMATCH = "kvno-mismatch"  # local kvno ≠ AD's stored kvno
STATUS_UNCHECKED = "unchecked"

# Thresholds (hours). Beyond 504h (21 days) we're approaching the 30d
# gMSA rotation and should be loud about it.
YELLOW_AGE_HOURS = 7 * 24       # 7 days
RED_AGE_HOURS = 21 * 24         # 21 days


@dataclass
class KeytabProbeResult:
    status: str
    message: str
    keytab_path: str
    keytab_mtime: Optional[str]
    keytab_principal: Optional[str]
    keytab_kvno_local: Optional[int]
    keytab_kvno_ad: Optional[int]
    kinit_ok: Optional[bool]
    kinit_error: Optional[str]


# ---------------------------------------------------------------------------
# Pure helpers (easy to fake in tests)
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _age_hours(mtime_iso: str) -> float:
    try:
        t = datetime.fromisoformat(mtime_iso)
    except ValueError:
        return float("inf")
    return (datetime.now(timezone.utc) - t).total_seconds() / 3600.0


def _classify_age(mtime_iso: Optional[str]) -> tuple[str, str]:
    """Return (status, message) based on keytab file age."""
    if not mtime_iso:
        return STATUS_UNCHECKED, "no mtime available"
    age = _age_hours(mtime_iso)
    if age < YELLOW_AGE_HOURS:
        return STATUS_OK, f"keytab is {age:.1f}h old"
    if age < RED_AGE_HOURS:
        return STATUS_STALE, (
            f"keytab is {age:.1f}h old; refresher cadence has slipped "
            f"(yellow at >{YELLOW_AGE_HOURS}h)"
        )
    return STATUS_BROKEN, (
        f"keytab is {age:.1f}h old — approaching 30-day gMSA rotation "
        f"(red at >{RED_AGE_HOURS}h)"
    )


# ---------------------------------------------------------------------------
# Probe — runs every sweep
# ---------------------------------------------------------------------------


def _run_cmd(argv: list[str], env: Optional[dict] = None,
             timeout: int = 30) -> tuple[int, str]:
    """Thin wrapper so tests can monkeypatch subprocess easily."""
    try:
        out = subprocess.run(
            argv, env=env, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as e:
        return (124, f"timeout after {timeout}s: {e}")
    combined = (out.stdout or "") + (out.stderr or "")
    return (out.returncode, combined)


def probe_keytab(*,
                 keytab_path: str,
                 principal: str,
                 ldap_host: str,
                 gmsa_dn: Optional[str] = None,
                 run_cmd: Callable = _run_cmd) -> KeytabProbeResult:
    """Full keytab health probe. Never raises — returns a
    :class:`KeytabProbeResult` with one of the STATUS_* values."""
    result = KeytabProbeResult(
        status=STATUS_UNCHECKED, message="",
        keytab_path=keytab_path, keytab_mtime=None,
        keytab_principal=None, keytab_kvno_local=None,
        keytab_kvno_ad=None, kinit_ok=None, kinit_error=None,
    )
    p = Path(keytab_path)
    if not p.exists():
        result.status = STATUS_MISSING
        result.message = f"keytab file not found at {keytab_path}"
        return result
    # File-age check.
    stat = p.stat()
    mtime_iso = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(
        timespec="seconds",
    )
    result.keytab_mtime = mtime_iso
    age_status, age_message = _classify_age(mtime_iso)

    # klist -k for local kvno + principal.
    rc, output = run_cmd(["klist", "-k", keytab_path])
    if rc == 0:
        for line in output.splitlines():
            parts = line.strip().split()
            if len(parts) == 2 and parts[0].isdigit():
                try:
                    result.keytab_kvno_local = int(parts[0])
                    result.keytab_principal = parts[1]
                    break
                except ValueError:
                    pass
    else:
        result.status = STATUS_BROKEN
        result.message = f"klist -k failed (rc={rc}): {output[:200]}"
        return result

    # kinit -kt — the strongest signal that the keytab's keys match
    # what the KDC has. Use an ephemeral ccache so we don't clobber
    # the sweep's AD-probe ccache.
    ephemeral_cc = f"FILE:/tmp/krb5cc_probe_{os.getpid()}"
    env = os.environ.copy()
    env["KRB5CCNAME"] = ephemeral_cc
    rc, output = run_cmd(
        ["kinit", "-kt", keytab_path, principal], env=env,
    )
    if rc == 0:
        result.kinit_ok = True
        # Clean up the cache.
        run_cmd(["kdestroy", "-c", ephemeral_cc], env=env)
    else:
        result.kinit_ok = False
        result.kinit_error = output[:500]
        result.status = STATUS_BROKEN
        result.message = f"kinit -kt failed (rc={rc}): {output[:200]}"
        return result

    # LDAP probe for AD-side kvno. Uses the ticket we just got, so
    # this also proves the gMSA can read its own object.
    if gmsa_dn:
        try:
            import ldap, ldap.sasl
            env_for_ldap = os.environ.copy()
            env_for_ldap["KRB5CCNAME"] = ephemeral_cc
            # Temporarily swap env for the ldap.initialize call — ldap
            # reads KRB5CCNAME at bind time.
            old_env = os.environ.get("KRB5CCNAME")
            os.environ["KRB5CCNAME"] = ephemeral_cc
            try:
                l = ldap.initialize(f"ldap://{ldap_host}")
                l.set_option(ldap.OPT_REFERRALS, 0)
                l.set_option(ldap.OPT_PROTOCOL_VERSION, 3)
                l.set_option(ldap.OPT_NETWORK_TIMEOUT, 10)
                l.sasl_interactive_bind_s("", ldap.sasl.gssapi())
                ad_res = l.search_s(
                    gmsa_dn, ldap.SCOPE_BASE,
                    "(objectClass=msDS-GroupManagedServiceAccount)",
                    ["msDS-KeyVersionNumber"],
                )
                l.unbind_s()
                if ad_res and ad_res[0][1].get("msDS-KeyVersionNumber"):
                    result.keytab_kvno_ad = int(
                        ad_res[0][1]["msDS-KeyVersionNumber"][0]
                    )
            finally:
                if old_env is None:
                    os.environ.pop("KRB5CCNAME", None)
                else:
                    os.environ["KRB5CCNAME"] = old_env
        except Exception as e:
            # The kvno compare is informational; don't fail the probe
            # if LDAP is blocked. The kinit already proved the keytab
            # works for auth.
            log.info("keytab kvno probe: LDAP lookup failed: %s", e)

    # KVNO mismatch is YELLOW — refresher wrote a stale key. Will
    # self-heal on the next refresh cycle.
    if (result.keytab_kvno_local is not None
            and result.keytab_kvno_ad is not None
            and result.keytab_kvno_local != result.keytab_kvno_ad):
        result.status = STATUS_KVNO_MISMATCH
        result.message = (
            f"local kvno {result.keytab_kvno_local} ≠ "
            f"AD kvno {result.keytab_kvno_ad} — refresher output is "
            "behind the gMSA's current password version"
        )
        return result

    # Age beats green.
    if age_status == STATUS_OK:
        result.status = STATUS_OK
        result.message = (
            f"ok · kvno {result.keytab_kvno_local} · "
            f"{result.keytab_principal} · {age_message}"
        )
    else:
        result.status = age_status
        result.message = age_message
    return result


def record_probe(db_path: Path, r: KeytabProbeResult) -> None:
    device_history_db.update_keytab_probe(
        db_path,
        keytab_path=r.keytab_path,
        keytab_mtime=r.keytab_mtime,
        keytab_principal=r.keytab_principal,
        keytab_kvno_local=r.keytab_kvno_local,
        keytab_kvno_ad=r.keytab_kvno_ad,
        last_probe_at=_now_iso(),
        last_probe_status=r.status,
        last_probe_message=r.message,
        last_kinit_at=_now_iso(),
        last_kinit_ok=r.kinit_ok,
        last_kinit_error=r.kinit_error,
    )


# ---------------------------------------------------------------------------
# Refresher — runs daily in the asyncio loop
# ---------------------------------------------------------------------------


def refresh_keytab(*,
                   db_path: Path,
                   kinit_principal: str,
                   kinit_password: str,
                   keytab_path: str,
                   gmsa_dn: str,
                   ldap_host: str,
                   realm: str,
                   gmsa_sam: str,
                   run_cmd: Callable = _run_cmd) -> tuple[bool, str]:
    """Kinit with ``kinit_principal`` (typically a Domain Admin whose
    password is stored in the vault), invoke the keytab generator, and
    update keytab_health.last_refresh_*.

    Returns ``(ok, message)``. Never raises.
    """
    # 1. kinit with the admin password. Use an ephemeral ccache so we
    # don't step on any ongoing sweep's ticket.
    cc = f"FILE:/tmp/krb5cc_refresh_{os.getpid()}"
    env = os.environ.copy()
    env["KRB5CCNAME"] = cc
    kinit_rc, kinit_out = run_cmd(
        ["kinit", kinit_principal],
        env=env,
        timeout=30,
    )
    if kinit_rc != 0:
        # kinit with stdin password.
        try:
            proc = subprocess.run(
                ["kinit", kinit_principal],
                env=env, input=kinit_password, text=True,
                capture_output=True, timeout=30,
            )
            if proc.returncode != 0:
                msg = (f"kinit {kinit_principal} failed: "
                       + (proc.stderr or proc.stdout or "").strip()[:300])
                device_history_db.update_keytab_refresh(
                    db_path, ok=False, message=msg,
                )
                return False, msg
        except Exception as e:
            msg = f"kinit subprocess failed: {e}"
            device_history_db.update_keytab_refresh(
                db_path, ok=False, message=msg,
            )
            return False, msg

    # 2. Call the keytab generator with the ephemeral ccache so it
    # binds as the admin principal.
    script_env = os.environ.copy()
    script_env.update({
        "KRB5CCNAME": cc,
        "KEYTAB_DC": ldap_host,
        "KEYTAB_REALM": realm,
        "KEYTAB_GMSA_SAM": gmsa_sam,
        "KEYTAB_GMSA_DN": gmsa_dn,
        "KEYTAB_PATH": keytab_path,
    })
    script = str(Path(__file__).resolve().parent.parent
                 / "scripts" / "ad" / "refresh_keytab.py")
    rc, out = run_cmd(
        [sys.executable, script], env=script_env, timeout=60,
    )
    # Clean up the ephemeral ccache.
    run_cmd(["kdestroy", "-c", cc], env=script_env)

    if rc != 0:
        msg = f"refresh_keytab.py failed (rc={rc}): {out[:500]}"
        device_history_db.update_keytab_refresh(
            db_path, ok=False, message=msg,
        )
        return False, msg

    msg = f"keytab rewritten at {keytab_path}"
    device_history_db.update_keytab_refresh(
        db_path, ok=True, message=msg,
    )
    return True, msg
