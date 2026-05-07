# Full UI Cockpit Redesign Implementation Spec

This implementation ports the accepted prototype in
`docs/specs/2026-05-06-full-ui-cockpit-mockup.html` into the existing
FastAPI + Jinja + vanilla JS app in vertical slices.

## Source Of Truth

- Visual direction: dark Ops Deck, command-forward, dense, animated, and
  readable.
- Prototype artifact: `docs/specs/2026-05-06-full-ui-cockpit-mockup.html`.
- Production rule: preserve existing URLs, form field names, API endpoints,
  noVNC imports, and backend action contracts.

## Preserved Routes

- `/`
- `/provision`
- `/template`
- `/sequences`
- `/sequences/new`
- `/sequences/{sequence_id}`
- `/runs/{run_id}`
- `/vms`
- `/devices/{vmid}`
- `/jobs`
- `/jobs/{job_id}`
- `/monitoring`
- `/monitoring/settings`
- `/cloud`
- `/hashes`
- `/answer-isos`
- `/credentials`
- `/credentials/new`
- `/credentials/{credential_id}`
- `/settings`
- `/auth/login`
- `/vms/{vmid}/console`

## Preserved Action Contracts

- `POST /api/jobs/provision`
- `POST /api/jobs/template`
- `POST /api/jobs/{job_id}/resume-template-build`
- Existing `/api/vms/*` power, status, console, key, and type endpoints
- Existing `/api/cloud/delete` and cloud-delete job status endpoints
- Existing `/api/monitoring/*` settings, OU, and keytab endpoints
- Existing credentials, sequences, artifacts, and settings forms/APIs

## Added Read/UX Helpers

- `GET /api/cockpit/summary` aggregates existing jobs, service health,
  fleet, and monitoring data for cockpit dashboard cards.
- `POST /api/monitoring/sweep-now` queues one monitor sweep through the
  existing monitor tick helper and returns `202`.

No database tables are added for the redesign.
