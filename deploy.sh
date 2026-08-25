#!/usr/bin/env bash
#
# Deploy ARCHIVE336 to the Hetzner production box.
#
# Run from the repo root:
#   ./deploy.sh           # backend + frontend
#   ./deploy.sh backend   # only backend
#   ./deploy.sh frontend  # only frontend
#
# Assumes you have already committed + pushed your changes to GitHub —
# the server pulls from there. (Frontend deploys rsync from local dist/.)

set -euo pipefail

# Host lives outside the repo. Publishing an origin IP defeats the
# Cloudflare proxy in front of it, so this reads from .deploy.local
# (gitignored) or the environment.
if [ -f "$(dirname "$0")/.deploy.local" ]; then
  # shellcheck disable=SC1091
  . "$(dirname "$0")/.deploy.local"
fi
SSH_HOST="${ARCHIVE336_SSH_HOST:?set ARCHIVE336_SSH_HOST in .deploy.local}"
SSH_KEY="$HOME/.ssh/aether_ed25519"
SSH_OPTS="-i $SSH_KEY -o StrictHostKeyChecking=accept-new"
SSH="ssh $SSH_OPTS $SSH_HOST"
DOMAIN="archive336.com"

mode="${1:-all}"
case "$mode" in
  all|backend|frontend) ;;
  *) echo "usage: $0 [all|backend|frontend]"; exit 2 ;;
esac

deploy_backend() {
  echo "==> Backend: pull on server"
  $SSH 'cd /opt/aether/app && git fetch --quiet origin && git reset --hard origin/main'

  # STACK.md is no longer in the repo - it carries the cost basis and
  # margins, which are not public even though the code is. The admin
  # panel still renders it from the repo root, so ship it separately
  # from the local working copy.
  echo "==> Backend: sync internal docs"
  rsync -az -e "ssh $SSH_OPTS" STACK.md "$SSH_HOST:/opt/aether/app/STACK.md" 2>/dev/null \
    || echo "    (STACK.md not found locally; admin Stack tab will be empty)"

  echo "==> Backend: install deps (if changed)"
  $SSH 'cd /opt/aether/app/backend && /opt/aether/venv/bin/pip install -q -r requirements.txt'

  echo "==> Backend: restart service"
  $SSH 'systemctl restart archive336-api && sleep 2 && systemctl is-active archive336-api'

  echo "==> Backend: health check"
  curl -sf "https://$DOMAIN/api/health" >/dev/null && echo "  /api/health: ok"
}

deploy_frontend() {
  echo "==> Frontend: build"
  npm run build

  echo "==> Frontend: rsync dist/ to server"
  rsync -az --delete -e "ssh $SSH_OPTS" dist/ "$SSH_HOST:/opt/aether/dist/"

  echo "==> Frontend: smoke check"
  status=$(curl -sI -o /dev/null -w "%{http_code}" "https://$DOMAIN/")
  echo "  /: HTTP $status"
}

if [[ "$mode" == "all" || "$mode" == "backend" ]]; then
  deploy_backend
fi
if [[ "$mode" == "all" || "$mode" == "frontend" ]]; then
  deploy_frontend
fi

echo "==> Done."
