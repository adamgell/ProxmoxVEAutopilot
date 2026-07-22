#!/usr/bin/env bash
# Deploy a released, committed image tag to the production controller (CT 500).
#
# Enforces the project rule: PRODUCTION IS ONLY UPDATED FROM COMMITS. It refuses
# to deploy unless the target is a git tag that exists on origin, aborts on a
# dirty working tree, and never uses the floating :latest image.
#
# Usage:
#   scripts/deploy_production.sh [vYYYY.MM.SEQ] [--yes] [--dry-run]
#
# With no tag argument it deploys v<contents of VERSION>. Override the target
# with env vars PROD_HOST, PROD_USER, PROD_COMPOSE_DIR.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PROD_HOST="${PROD_HOST:-192.168.2.4}"
PROD_USER="${PROD_USER:-root}"
PROD_COMPOSE_DIR="${PROD_COMPOSE_DIR:-/opt/ProxmoxVEAutopilot/autopilot-proxmox}"

TAG=""
ASSUME_YES=0
DRY_RUN=0
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --yes|-y) ASSUME_YES=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --force) FORCE=1 ;;
    v*) TAG="$arg" ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

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
#     commit prod currently runs, so we don't clobber newer/unmerged work. ---
PROD_SHA="$(ssh "${PROD_USER}@${PROD_HOST}" "curl -fsS http://localhost:5000/api/version" 2>/dev/null \
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

if [ "$DRY_RUN" -eq 1 ]; then
  echo "[dry-run] would pin AUTOPILOT_IMAGE_TAG=$TAG and run docker compose pull + up -d on $PROD_HOST."
  exit 0
fi

if [ "$ASSUME_YES" -ne 1 ]; then
  read -r -p "Deploy $TAG to production ${PROD_HOST}? [y/N] " reply
  case "$reply" in y|Y|yes|YES) ;; *) echo "Cancelled."; exit 1 ;; esac
fi

# --- Deploy: sync the committed compose (so the host honors the pinned tag
#     instead of a stale :latest), pin the tag in .env, pull, up -d ---
echo "==> Syncing committed docker-compose.yml to ${PROD_HOST} ..."
scp -q autopilot-proxmox/docker-compose.yml "${PROD_USER}@${PROD_HOST}:${PROD_COMPOSE_DIR}/docker-compose.yml"
echo "==> Deploying on ${PROD_HOST} ..."
ssh "${PROD_USER}@${PROD_HOST}" bash -s -- "$PROD_COMPOSE_DIR" "$TAG" <<'REMOTE'
set -euo pipefail
DIR="$1"; TAG="$2"
cd "$DIR"
touch .env
{ grep -v '^AUTOPILOT_IMAGE_TAG=' .env 2>/dev/null || true; echo "AUTOPILOT_IMAGE_TAG=$TAG"; } > .env.tmp
mv .env.tmp .env
# Preserve the current autopilot-builder replica count: it scales via the CLI
# --scale flag (no compose `replicas`), so a plain `up -d` would drop it to 1.
N=$(docker compose ps -q autopilot-builder 2>/dev/null | wc -l | tr -d ' ')
[ "${N:-0}" -ge 1 ] || N=1
docker compose pull    # fails here if the tagged image is not built/published yet
docker compose up -d --scale autopilot-builder="$N"
REMOTE

# --- Verify the running version ---
echo "==> Verifying /api/version on ${PROD_HOST} ..."
sleep 5
REMOTE_VER="$(ssh "${PROD_USER}@${PROD_HOST}" "curl -fsS http://localhost:5000/api/version" 2>/dev/null || true)"
echo "$REMOTE_VER"
if printf '%s' "$REMOTE_VER" | grep -Eq "\"version\"[[:space:]]*:[[:space:]]*\"$EXPECTED_VERSION\""; then
  echo "==> OK: production is running $EXPECTED_VERSION ($TAG)"
else
  echo "WARN: could not confirm version $EXPECTED_VERSION from /api/version yet (it may still be starting)." >&2
fi
