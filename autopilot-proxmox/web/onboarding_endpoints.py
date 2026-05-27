"""FastAPI router for the operator onboarding wizard."""
from __future__ import annotations

import threading
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel

from web import auth, db_pg, onboarding_pg


router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])


def _owner_sub(user: dict = Depends(auth.current_user)) -> str:
    """Server-derived owner_sub. Local-auth mode returns 'local-operator'."""
    sub = user.get("sub")
    if not sub:
        return "local-operator"
    return sub


class PutStateRequest(BaseModel):
    patch: dict[str, Any] = {}


@router.get("/state")
def get_state(response: Response, owner_sub: str = Depends(_owner_sub)):
    with db_pg.connection() as conn:
        row = onboarding_pg.get_state(conn, owner_sub)
    if row is None:
        raise HTTPException(status_code=404, detail="no onboarding row")
    response.headers["ETag"] = row["etag"]
    return _scrub_for_client(row)


@router.put("/state")
def put_state(
    body: PutStateRequest,
    response: Response,
    owner_sub: str = Depends(_owner_sub),
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    # Intake: pull raw secret values out of the patch, write them to vault.yml,
    # rewrite the patch with the sentinel ref shape. Secrets never reach the DB row.
    sanitized = _intake_secrets(owner_sub, body.patch)
    with db_pg.connection() as conn:
        existing = onboarding_pg.get_state(conn, owner_sub)
        if existing is not None and if_match is None:
            raise HTTPException(status_code=428, detail="If-Match required for updates")
        try:
            row = onboarding_pg.put_state(
                conn, owner_sub=owner_sub, if_match=if_match, patch=sanitized
            )
        except onboarding_pg.StaleEtag as e:
            raise HTTPException(status_code=409, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    response.headers["ETag"] = row["etag"]
    return _scrub_for_client(row)


def _intake_secrets(owner_sub: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Pull raw secret values out of the patch, write them to vault.yml,
    rewrite the patch with sentinel refs. Mutates a copy; original patch is untouched."""
    from web import app as web_app  # vault lives in web.app
    out = dict(patch)
    identity = dict(out.get("identity") or {})
    for raw_key, ref_key, vault_key in [
        ("ad_join_password", "ad_join_password_ref", f"onboarding/{owner_sub}/ad_join_password"),
        ("local_admin_password", "local_admin_password_ref", f"onboarding/{owner_sub}/local_admin_password"),
    ]:
        raw = identity.pop(raw_key, None)
        if raw is None or raw == "":
            continue
        web_app._save_vault({vault_key: raw})
        identity[ref_key] = f"vault:{vault_key}"
    if "identity" in out:
        out["identity"] = identity
    return out


@router.delete("/state", status_code=204)
def delete_state(owner_sub: str = Depends(_owner_sub)):
    with db_pg.connection() as conn:
        onboarding_pg.delete_state(conn, owner_sub)
    return Response(status_code=204)


def _scrub_for_client(row: dict) -> dict:
    """Strip secret values; emit {ref, is_set} for *_password_ref fields."""
    answers = dict(row.get("answers") or {})
    identity = dict(answers.get("identity") or {})
    for key in ("ad_join_password_ref", "local_admin_password_ref"):
        ref = identity.get(key)
        if isinstance(ref, dict):
            continue  # already scrubbed
        if isinstance(ref, str) and ref.startswith("vault:"):
            identity[key] = {"ref": ref, "is_set": True}
        elif ref:
            identity[key] = {"ref": None, "is_set": True}
        else:
            identity[key] = {"ref": None, "is_set": False}
    answers["identity"] = identity
    out = dict(row)
    out["answers"] = answers
    return out


_PROBE_LOCKS: dict[tuple[str, str], threading.Lock] = {}


def _probe_lock(owner_sub: str, probe_name: str):
    """Per-(owner_sub, probe_name) in-process lock to rate-limit probes.

    Spec: 'Probe endpoints are rate-limited to one in-flight call per
    (owner_sub, probe-name) pair via an in-process lock.' Returns a context
    manager. Raises HTTPException(429) if a call is already in flight.
    """
    key = (owner_sub, probe_name)
    lock = _PROBE_LOCKS.setdefault(key, threading.Lock())
    acquired = lock.acquire(blocking=False)
    if not acquired:
        raise HTTPException(status_code=429, detail=f"probe {probe_name} already in flight")

    class _Releaser:
        def __enter__(self): return self
        def __exit__(self, *a): lock.release()

    return _Releaser()


@router.get("/already-configured")
def already_configured():
    """Aggregator for the wizard's step 1 'Already configured' card.

    Reads Proxmox version + storages + bridges from the existing Proxmox client,
    and AD vault presence from app._load_vault. Each row is independent: a failure
    in one does not poison the others.
    """
    out = {
        "proxmox": {"ok": False, "summary": "not checked"},
        "storage": {"ok": False, "summary": "not checked"},
        "network": {"ok": False, "summary": "not checked"},
        "ad_vault": {"ok": False, "summary": "not checked"},
    }
    # Proxmox version + node listing. Reuse the existing _proxmox_api helper from app.py.
    try:
        from web import app as web_app
        nodes = web_app._proxmox_api("/nodes")
        if nodes:
            node = nodes[0]
            out["proxmox"] = {"ok": True, "summary": f"connected to {node['node']} v{node.get('version', '?')}"}
            try:
                storages = web_app._proxmox_api(f"/nodes/{node['node']}/storage")
                out["storage"] = {"ok": True, "summary": f"{len(storages)} storage pools on {node['node']}"}
            except Exception as e:
                out["storage"] = {"ok": False, "summary": f"could not list storages: {e}"}
            try:
                bridges = web_app._proxmox_api(f"/nodes/{node['node']}/network")
                bridge_names = [b["iface"] for b in bridges if b.get("type") == "bridge"]
                out["network"] = {"ok": True, "summary": f"bridges: {', '.join(bridge_names) or '(none)'}"}
            except Exception as e:
                out["network"] = {"ok": False, "summary": f"could not list bridges: {e}"}
    except Exception as e:
        out["proxmox"] = {"ok": False, "summary": f"Couldn't reach Proxmox: {e}"}
    # AD vault status via _load_vault in app.py.
    try:
        from web import app as web_app
        vault = web_app._load_vault()
        present = bool(vault.get("vault_ad_join_password"))
        out["ad_vault"] = {
            "ok": present,
            "summary": "AD service account password set" if present else "no AD password vaulted yet",
        }
    except Exception as e:
        out["ad_vault"] = {"ok": False, "summary": f"vault check failed: {e}"}
    return out


# Probe + launch + setup-status: stubs for now (Tasks 7-12 will implement).
class ProbeAdRequest(BaseModel):
    domain: str
    account: str
    password: str


@router.post("/probe/ad")
def probe_ad(body: ProbeAdRequest, owner_sub: str = Depends(_owner_sub)):
    from web import onboarding_probes
    with _probe_lock(owner_sub, "ad"):
        return onboarding_probes.probe_ad(body.domain, body.account, body.password)


class ProbeTenantRequest(BaseModel):
    tenant_id: str
    tenant_domain: str
    graph_check: bool = True


@router.post("/probe/tenant")
def probe_tenant(body: ProbeTenantRequest, owner_sub: str = Depends(_owner_sub)):
    from web import onboarding_probes
    with _probe_lock(owner_sub, "tenant"):
        return onboarding_probes.probe_tenant(body.tenant_id, body.tenant_domain, graph_check=body.graph_check)


@router.post("/probe/artifact")
def probe_artifact(owner_sub: str = Depends(_owner_sub)):
    from web import onboarding_probes
    with _probe_lock(owner_sub, "artifact"):
        return onboarding_probes.probe_artifact()


@router.post("/launch", status_code=501)
def launch():
    raise HTTPException(status_code=501, detail="launch not yet implemented")


@router.get("/setup-status", status_code=501)
def setup_status():
    raise HTTPException(status_code=501, detail="setup-status not yet implemented")
