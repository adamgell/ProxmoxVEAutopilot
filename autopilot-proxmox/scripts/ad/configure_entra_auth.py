"""Idempotent configuration of the existing Entra app registration
for browser-side OIDC login.

Before running, the app reg has Graph delegated perms for Intune /
Autopilot workflows + a client secret we use in vault.yml for
Graph-token requests. This script adds what we need on top:

* A redirect URI pointing at this deployment's /auth/callback.
* Enables ID-token issuance (Authorization Code + PKCE flow).
* Adds delegated permissions: openid, profile, email (needed to
  get name + email in the ID token claims).

Run from inside the autopilot container:

    python3 scripts/ad/configure_entra_auth.py \\
        --redirect-uri https://autopilot.gell.one/auth/callback

Writes nothing to vault.yml — the operator picks up the redirect
URI from the env/vault of their deployment. Admin consent for the
new scopes may need a one-click confirm in the Azure portal since
these are delegated (user-consentable), but Entra usually
auto-consents openid/profile/email for a tenant's own users.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests


# Microsoft Graph resource + well-known scope IDs (constant per Microsoft).
GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
SCOPE_IDS = {
    "openid":  "37f7f235-527c-4136-accd-4a02d197296e",
    "profile": "14dad69e-099b-42c9-810b-d002981feec1",
    "email":   "64a6cdd6-aab1-4aaf-94b8-3cc8405e90d0",
}


def token(tenant: str, client_id: str, client_secret: str) -> str:
    r = requests.post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id, "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default",
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def find_app(headers: dict, app_id: str) -> dict:
    r = requests.get(
        f"https://graph.microsoft.com/v1.0/applications?$filter=appId eq '{app_id}'",
        headers=headers, timeout=15,
    )
    r.raise_for_status()
    apps = r.json().get("value", [])
    if not apps:
        raise RuntimeError(f"app registration not found: appId={app_id}")
    return apps[0]


def configure(headers: dict, app: dict, *, redirect_uri: str) -> dict:
    """Returns the list of changes applied. Idempotent — already-configured
    settings are left alone."""
    app_obj_id = app["id"]
    web = app.get("web", {}) or {}
    current_uris = list(web.get("redirectUris") or [])
    current_implicit = dict(web.get("implicitGrantSettings") or {})
    current_rras = list(app.get("requiredResourceAccess") or [])

    new_web = dict(web)
    changes = []

    # 1. Add redirect URI.
    if redirect_uri not in current_uris:
        new_web["redirectUris"] = current_uris + [redirect_uri]
        changes.append(f"+ redirectUri: {redirect_uri}")

    # 2. Enable ID token issuance (auth-code + PKCE doesn't strictly
    # require this on newer tenants, but turning it on avoids the
    # "implicit grant disabled" error path in some client libs).
    if not current_implicit.get("enableIdTokenIssuance"):
        new_web["implicitGrantSettings"] = {
            **current_implicit,
            "enableIdTokenIssuance": True,
            "enableAccessTokenIssuance": current_implicit.get(
                "enableAccessTokenIssuance", False,
            ),
        }
        changes.append("+ enableIdTokenIssuance = True")

    # 3. Add openid/profile/email delegated permissions.
    # Find the Graph resource entry (may not exist yet for this app).
    graph_rra = next(
        (r for r in current_rras if r.get("resourceAppId") == GRAPH_APP_ID),
        None,
    )
    if graph_rra is None:
        graph_rra = {"resourceAppId": GRAPH_APP_ID, "resourceAccess": []}
        current_rras.append(graph_rra)

    existing_scope_ids = {
        p["id"] for p in graph_rra.get("resourceAccess", [])
        if p.get("type") == "Scope"
    }
    added_any_scope = False
    for name, sid in SCOPE_IDS.items():
        if sid in existing_scope_ids:
            continue
        graph_rra.setdefault("resourceAccess", []).append(
            {"id": sid, "type": "Scope"},
        )
        changes.append(f"+ delegated scope: {name}")
        added_any_scope = True

    # Nothing to do?
    if not changes:
        return {"changes": [], "app_object_id": app_obj_id}

    patch_body: dict = {}
    if new_web != web:
        patch_body["web"] = new_web
    if added_any_scope:
        patch_body["requiredResourceAccess"] = current_rras
    r = requests.patch(
        f"https://graph.microsoft.com/v1.0/applications/{app_obj_id}",
        headers={**headers, "Content-Type": "application/json"},
        data=json.dumps(patch_body), timeout=30,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"PATCH failed [{r.status_code}]: {r.text[:500]}")
    return {"changes": changes, "app_object_id": app_obj_id}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--redirect-uri", required=True,
                        help="OIDC callback URL, e.g. https://autopilot.gell.one/auth/callback")
    parser.add_argument("--tenant", default=None,
                        help="override vault tenant id")
    parser.add_argument("--client-id", default=None,
                        help="override vault app id")
    parser.add_argument("--client-secret", default=None,
                        help="override vault client secret")
    args = parser.parse_args()

    # Load from vault by default so operators just need --redirect-uri.
    if not (args.tenant and args.client_id and args.client_secret):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        try:
            from web.app import _load_proxmox_config
        except ImportError:
            print("run from inside the autopilot container OR pass "
                  "--tenant / --client-id / --client-secret", file=sys.stderr)
            return 2
        cfg = _load_proxmox_config()
        args.tenant = args.tenant or cfg["vault_entra_tenant_id"]
        args.client_id = args.client_id or cfg["vault_entra_app_id"]
        args.client_secret = args.client_secret or cfg["vault_entra_app_secret"]

    t = token(args.tenant, args.client_id, args.client_secret)
    headers = {"Authorization": f"Bearer {t}"}
    app = find_app(headers, args.client_id)
    print(f"app: {app['displayName']} ({app['id']})")

    result = configure(headers, app, redirect_uri=args.redirect_uri)
    if not result["changes"]:
        print("no changes needed — app reg already configured")
    else:
        print("applied:")
        for c in result["changes"]:
            print(f"  {c}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
