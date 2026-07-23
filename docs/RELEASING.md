# Releasing and Production Deploys

## The rule

**Production is only ever updated from commits.** The production controller
(CT 500 at `192.168.2.4`) runs an immutable, CI-built image tag that corresponds
to a git tag on `origin`. It never runs the floating `:latest` tag, and it is
never deployed from a dirty or unpushed working tree. `scripts/deploy_production.sh`
enforces this mechanically.

## Versioning

- **Scheme:** CalVer `YYYY.MM.SEQ` (e.g. `2026.07.0`). `SEQ` starts at `0` each
  month and increments per release within that month.
- **Single source of truth:** the repo-root [`VERSION`](../VERSION) file. It is
  unified across the whole monorepo (web/docker control-plane, frontend, and the
  AutopilotAgent).
- The AutopilotAgent uses a normalized form for .NET assembly versions
  (`2026.07.0` -> `2026.7.0`) in `autopilot-agent/Directory.Build.props`;
  `scripts/bump_version.sh` keeps both in sync.

### Where the version shows up

- Baked into the docker image at build time as `APP_VERSION` (build-arg) and
  reported by the running app at `GET /api/version` as
  `{"running": {"version": "2026.07.0", "sha": "...", "build_time": "..."}}`.
- Images are tagged in GHCR as `:v2026.07.0` (immutable), plus `:<sha>` and
  `:latest` (latest tracks `main` only).

## Release flow

```sh
# 1. Bump the version (auto-computes the next SEQ for this month, or pass one)
scripts/bump_version.sh                 # or: scripts/bump_version.sh 2026.08.0

# 2. Commit + push the bump
git add VERSION autopilot-agent/Directory.Build.props
git commit -m "release: v2026.07.1"
git push origin main

# 3. Tag the release and push the tag -> CI builds ghcr .../:v2026.07.1
git tag v2026.07.1
git push origin v2026.07.1

# 4. Deploy that exact tag to production (after the image build finishes)
scripts/deploy_production.sh v2026.07.1
```

## How the deploy guard works

`scripts/deploy_production.sh [vX.Y.Z] [--yes] [--dry-run]`:

1. Aborts if the working tree is dirty.
2. Aborts unless the target git tag exists **locally and on `origin`** (proof it
   is committed and pushed).
3. Aborts if prod's current commit (read from `/api/version`) is not an ancestor
   of the target tag - i.e. deploying would revert commits prod already has
   (override with `--force`).
4. SSHes to the production host, records the tag being replaced as
   `AUTOPILOT_IMAGE_TAG_PREV` (for rollback), upserts `AUTOPILOT_IMAGE_TAG=<tag>`
   into the compose `.env`, then `docker compose pull` (fails fast if the tagged
   image was not published) + `docker compose up -d` (preserving the builder
   replica count).
5. **Health-gates the deploy:** polls `GET /healthz` (the readiness endpoint) for
   up to ~90s and **fails non-zero** if the app never returns 200 - a broken
   deploy is no longer reported as success. Then it version-matches `/api/version`.

### Rollback

`scripts/deploy_production.sh --rollback [--yes]` redeploys the
`AUTOPILOT_IMAGE_TAG_PREV` recorded on the host during the last deploy. It is an
intentional revert, so it bypasses the ancestor guard and health-gates the
rolled-back image the same way. If `/healthz` fails after a deploy, the script
tells you to run exactly this.

`docker-compose.yml` pins `image: ghcr.io/adamgell/proxmox-autopilot:${AUTOPILOT_IMAGE_TAG:-v2026.07.0}`
so there is no floating `:latest` in production. Bump the committed default when
cutting a new release.

## Note on the in-app [Update] button

The footer's [Update] button (`/api/update/run`) spawns a sidecar that does
`git pull` + a local `docker build`. That path does **not** work on the current
production host, because `/opt/ProxmoxVEAutopilot` there is a synced copy, not a
git checkout. The real, supported deploy path is the image-pull in
`scripts/deploy_production.sh` (pull the CI-built tagged image + `up -d`). Treat
the button as non-functional here until the host becomes a real checkout.

## CI

`.github/workflows/docker-publish.yml` builds on every push to `main`
(`:<sha>` + `:latest`) and on every `v*` tag (`:v<CalVer>`), passing
`APP_VERSION` from the `VERSION` file. The `paths` filter was removed so a
release commit that only bumps `VERSION` still triggers the tagged build.
