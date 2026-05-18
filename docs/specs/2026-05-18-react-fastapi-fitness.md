# React Migration FastAPI Fitness Check

Date: 2026-05-18
Decision: keep FastAPI as the API, auth, static-asset, and WebSocket host for the React migration.

## Findings

- FastAPI already owns the production contracts that React needs to consume: session auth, `/api/*` JSON endpoints, `/api/live/ws`, VNC WebSocket proxying, and the agent/WinPE/CloudOSD/OSDeploy protocol surfaces.
- The existing auth middleware can keep the React HTML fallback protected while `/static/react/*` remains public for hashed build assets. No new broad exemption is needed for `/app/*` or `/openapi.json`.
- OpenAPI export should remain local-only. `autopilot-proxmox/scripts/export_openapi_schema.py` imports `web.app` under `AUTOPILOT_AUTH_BYPASS=1` and writes a schema file for frontend code generation without exposing live schema access.
- The first schema export already shows duplicate operation-id warnings for CloudOSD and OSDeploy cache download endpoints. That does not block the shell slice, but it should be cleaned up before relying heavily on generated API clients for those surfaces.
- `web/app.py` is large enough that future React route migrations should also move route groups toward existing routers or new routers when touching a surface. That is incremental hardening, not a reason to replace FastAPI.
- Docker can serve the React build without adding Node to the runtime image by using a Node build stage and copying `frontend/dist` into `/app/web/static/react`.

## Follow-Up Hardening

- Add explicit `response_model` coverage to React-facing API endpoints as each route migrates.
- Add stable operation IDs to endpoints with duplicate OpenAPI names before generated clients become a blocking contract.
- Promote typed WebSocket message definitions as each live topic is consumed by React.
- Keep login, setup, bootstrap, WinPE, agent, CloudOSD PE, OSDeploy PE, setup artifact, and MCP protocol endpoints outside the React migration unless a later plan explicitly includes them.
