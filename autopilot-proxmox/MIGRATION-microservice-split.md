# Migrating to the microservice-split release

**Breaking change:** the `autopilot` docker-compose service is now three
services: `autopilot` (web), `autopilot-builder`, and `autopilot-monitor`.

## Before upgrading

Finish any running jobs. In-flight jobs at migration time will be
orphaned (their subprocess dies when the old web container restarts).
Operators can see orphaned jobs in `/jobs` with status "orphaned"
after the upgrade.

## Upgrade steps

1. Pull the new image in the UI (footer → Update button) or manually:
   ```
   docker compose pull
   ```
2. Replace your `docker-compose.yml` with the new three-service version
   from this repo. Vault / vars / secrets mounts are the same.
3. Restart:
   ```
   docker compose up -d
   ```
4. On first boot, the web container migrates `jobs/index.json` to
   `output/jobs.db` and renames the legacy file
   `jobs/index.json.pre-split.bak`. Back up the .bak file if you want
   to keep legacy job history.

## Scaling builders

To run N parallel builders:
```
docker compose up -d --scale autopilot-builder=3
```

Per-job-type caps in `/settings → Job concurrency` still apply.

## Rolling back

Older single-service compose files keep working with the new image
since the entrypoint defaults to `web`. Revert compose, keep the new
image — web will run as before, but builder/monitor won't, so jobs will
pile up as "pending". Downgrade the image alongside the compose rollback
to restore full single-container behavior.
