#!/usr/bin/env bash
# Deploy a released, committed image tag to the production controller (CT 500).
#
# Enforces the project rule: PRODUCTION IS ONLY UPDATED FROM COMMITS. It refuses
# to deploy unless the target is a git tag that exists on origin, aborts on a
# dirty working tree, and never uses the floating :latest image. After deploy it
# gates on /healthz and fails (non-zero) if the app never becomes healthy.
#
# Usage:
#   scripts/deploy_production.sh [vYYYY.MM.SEQ] [--yes] [--dry-run] [--force]
#   scripts/deploy_production.sh --rollback [--yes]   # redeploy the previous tag
#
# With no tag argument it deploys v<contents of VERSION>. Override the target
# with env vars PROD_HOST, PROD_USER, PROD_COMPOSE_DIR.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PROD_HOST="${PROD_HOST:-192.168.2.4}"
PROD_USER="${PROD_USER:-root}"
PROD_COMPOSE_DIR="${PROD_COMPOSE_DIR:-/opt/ProxmoxVEAutopilot/autopilot-proxmox}"
API_URL="http://localhost:5000"

TAG=""
ASSUME_YES=0
DRY_RUN=0
FORCE=0
ROLLBACK=0
for arg in "$@"; do
  case "$arg" in
    --yes|-y) ASSUME_YES=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --force) FORCE=1 ;;
    --rollback) ROLLBACK=1 ;;
    v*) TAG="$arg" ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

if [ "$ROLLBACK" -eq 1 ]; then
  # Redeploy the previously-recorded tag from the host .env. This is an
  # intentional revert, so it bypasses the ancestor/superset guard by design.
  TAG="$(ssh "${PROD_USER}@${PROD_HOST}" \
    "grep '^AUTOPILOT_IMAGE_TAG_PREV=' ${PROD_COMPOSE_DIR}/.env 2>/dev/null | tail -1 | cut -d= -f2-" \
    2>/dev/null || true)"
  if [ -z "$TAG" ]; then
    echo "ABORT: no AUTOPILOT_IMAGE_TAG_PREV recorded on ${PROD_HOST}; nothing to roll back to." >&2
    exit 1
  fi
  EXPECTED_VERSION="${TAG#v}"
  echo "==> ROLLBACK target: $TAG  (previously-deployed tag on ${PROD_HOST})"
  echo "==> Production host: ${PROD_USER}@${PROD_HOST}:${PROD_COMPOSE_DIR}"
else
  [ -n "$TAG" ] || TAG="v$(tr -d '[:space:]' < VERSION)"
  EXPECTED_VERSION="${TAG#v}"
  echo "==> Target release:  $TAG  (version $EXPECTED_VERSION)"
  echo "==> Production host: ${PROD_USER}@${PROD_HOST}:${PROD_COMPOSE_DIR}"

  # --- Guard 1: clean working tree ---
  if [ -n "$(git status --porcelain)" ]; then
    echo "ABORT: working tree is dirty. Production only deploys from committed state." >&2
    git status --short >&2
    exit 1
  fi

  # --- Guard 2: the tag must exist locally AND on origin (committed + pushed) ---
  git fetch --tags --quiet origin || true
  if ! git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
    echo "ABORT: git tag $TAG does not exist. Create + push it: git tag $TAG && git push origin $TAG" >&2
    exit 1
  fi
  if ! git ls-remote --exit-code --tags origin "refs/tags/$TAG" >/dev/null 2>&1; then
    echo "ABORT: git tag $TAG is not on origin. Push it: git push origin $TAG" >&2
    exit 1
  fi
  TAG_SHA="$(git rev-parse "refs/tags/$TAG^{commit}")"
  echo "==> $TAG -> commit ${TAG_SHA:0:12} (present on origin)"

  # --- Guard 3: never revert prod. The target must be a descendant of whatever
  #     commit prod currently runs, so we don't clobber newer/unmerged work.
  #     (Use --rollback to intentionally go backwards.) ---
  PROD_SHA="$(ssh "${PROD_USER}@${PROD_HOST}" "curl -fsS ${API_URL}/api/version" 2>/dev/null \
    | sed -n 's/.*"sha"[[:space:]]*:[[:space:]]*"\([0-9a-f]\{7,40\}\)".*/\1/p' | head -1 || true)"
  if [ -n "$PROD_SHA" ]; then
    if git cat-file -e "${PROD_SHA}^{commit}" 2>/dev/null; then
      if git merge-base --is-ancestor "$PROD_SHA" "$TAG_SHA"; then
        echo "==> prod is at ${PROD_SHA:0:12}; $TAG is a safe superset."
      else
        echo "ABORT: prod runs ${PROD_SHA:0:12}, which is NOT an ancestor of $TAG." >&2
        echo "       Deploying would revert commits prod already has. Reconcile first, or pass --force." >&2
        [ "$FORCE" -eq 1 ] || exit 1
      fi
    else
      echo "WARN: prod sha ${PROD_SHA:0:12} is unknown locally (unpushed?). Fetch/reconcile it, or pass --force." >&2
      [ "$FORCE" -eq 1 ] || exit 1
    fi
  else
    echo "WARN: could not read prod's current sha from /api/version; skipping the superset check." >&2
  fi
fi

if [ "$DRY_RUN" -eq 1 ]; then
  echo "[dry-run] would deploy image ...:$TAG (rollback=$ROLLBACK) and gate on /healthz."
  exit 0
fi

if [ "$ASSUME_YES" -ne 1 ]; then
  prompt="Deploy $TAG to production ${PROD_HOST}?"
  [ "$ROLLBACK" -eq 1 ] && prompt="ROLL BACK production ${PROD_HOST} to $TAG?"
  read -r -p "$prompt [y/N] " reply
  case "$reply" in y|Y|yes|YES) ;; *) echo "Cancelled."; exit 1 ;; esac
fi

# --- Deploy: sync the committed compose (so the host honors the pinned tag
#     instead of a stale :latest), record the prior tag for rollback, pin the
#     new tag in .env, pull, up -d (preserving builder scale). ---
echo "==> Syncing committed docker-compose.yml to ${PROD_HOST} ..."
scp -q autopilot-proxmox/docker-compose.yml "${PROD_USER}@${PROD_HOST}:${PROD_COMPOSE_DIR}/docker-compose.yml"
echo "==> Deploying on ${PROD_HOST} ..."
ssh "${PROD_USER}@${PROD_HOST}" bash -s -- "$PROD_COMPOSE_DIR" "$TAG" <<'REMOTE'
set -euo pipefail
DIR="$1"; TAG="$2"
cd "$DIR"
touch .env
CUR="$(grep '^AUTOPILOT_IMAGE_TAG=' .env 2>/dev/null | tail -1 | cut -d= -f2- || true)"
{
  grep -vE '^AUTOPILOT_IMAGE_TAG(_PREV)?=' .env 2>/dev/null || true
  # Record the tag we are replacing so `--rollback` can return to it.
  if [ -n "$CUR" ] && [ "$CUR" != "$TAG" ]; then echo "AUTOPILOT_IMAGE_TAG_PREV=$CUR"; fi
  echo "AUTOPILOT_IMAGE_TAG=$TAG"
} > .env.tmp
mv .env.tmp .env
# Preserve the current autopilot-builder replica count: it scales via the CLI
# --scale flag (no compose `replicas`), so a plain `up -d` would drop it to 1.
N=$(docker compose ps -q autopilot-builder 2>/dev/null | wc -l | tr -d ' ')
[ "${N:-0}" -ge 1 ] || N=1
docker compose pull    # fails here if the tagged image is not built/published yet
docker compose up -d --scale autopilot-builder="$N"
REMOTE

# --- Health-gate: poll /healthz (the readiness endpoint gating on both startup
#     hooks) respecting the compose start_period (~20s). Fail the deploy if it
#     never becomes healthy. ---
echo "==> Waiting for ${API_URL}/healthz on ${PROD_HOST} ..."
healthy=0
for i in $(seq 1 30); do
  code="$(ssh "${PROD_USER}@${PROD_HOST}" "curl -fsS -o /dev/null -w '%{http_code}' ${API_URL}/healthz" 2>/dev/null || echo 000)"
  if [ "$code" = "200" ]; then healthy=1; echo "==> /healthz 200 after ~$((i * 3))s"; break; fi
  sleep 3
done
if [ "$healthy" -ne 1 ]; then
  echo "ERROR: ${API_URL}/healthz never returned 200 (~90s) after deploying $TAG." >&2
  echo "       Production is NOT healthy. Roll back with: scripts/deploy_production.sh --rollback --yes" >&2
  exit 1
fi

# --- Verify the running version matches the target. ---
REMOTE_VER="$(ssh "${PROD_USER}@${PROD_HOST}" "curl -fsS ${API_URL}/api/version" 2>/dev/null || true)"
echo "$REMOTE_VER"
if printf '%s' "$REMOTE_VER" | grep -Eq "\"version\"[[:space:]]*:[[:space:]]*\"$EXPECTED_VERSION\""; then
  echo "==> OK: production is healthy and running $EXPECTED_VERSION ($TAG)"
else
  echo "WARN: /healthz is green but /api/version did not report $EXPECTED_VERSION." >&2
fi
