#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# I'M IN — social-automation bot deploy to VPS
#
# Rsyncs the bot source to /opt/imin-bot on the target server,
# then rebuilds and restarts only the imin-bot service inside the
# main /opt/imin docker-compose stack (postgres, redis, settings-api,
# nginx etc. are untouched).
#
# Usage:
#   bash scripts/bot-deploy.sh <server-ip> [ssh-port] [user]
#
# Pre-requisites on the server:
#   - /opt/imin/docker-compose.yml already contains the imin-bot service
#     (added in imin-backend repo).
#   - /opt/imin/.env contains IMIN_BOT_PG_PASSWORD and all bot API keys
#     (TELEGRAM_BOT_TOKEN, OPENAI_API_KEY, PERPLEXITY_API_KEY, ...).
#   - The Postgres database `imin_bot` exists (created by
#     postgres/init/200-imin-bot-db.sh on first init, or manually via
#     psql for existing clusters).
# ============================================================

SERVER="${1:?Usage: bot-deploy.sh <server-ip> [ssh-port] [user]}"
SSH_PORT="${2:-22}"
USER="${3:-root}"
REMOTE_DIR="/opt/imin-bot"
COMPOSE_DIR="/opt/imin"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== Deploying I'M IN bot to $USER@$SERVER ==="

echo "[0/4] Checking SSH..."
if ! ssh -p "$SSH_PORT" -o ConnectTimeout=10 "$USER@$SERVER" "echo ok" >/dev/null 2>&1; then
  echo "ERROR: cannot SSH to $USER@$SERVER:$SSH_PORT"
  exit 1
fi
echo "  -> OK"

echo "[1/4] Syncing source to $REMOTE_DIR..."
ssh -p "$SSH_PORT" "$USER@$SERVER" "mkdir -p $REMOTE_DIR"
rsync -avz --delete \
  --exclude '.git' \
  --exclude '__pycache__' \
  --exclude '.env' \
  --exclude '.env.example' \
  --exclude 'data/' \
  --exclude 'media_cache/' \
  --exclude '*.pyc' \
  -e "ssh -p $SSH_PORT" \
  "$PROJECT_DIR/" "$USER@$SERVER:$REMOTE_DIR/"

echo "[2/4] Validating compose..."
ssh -p "$SSH_PORT" "$USER@$SERVER" "
  cd $COMPOSE_DIR
  if ! grep -q 'imin-bot:' docker-compose.yml; then
    echo 'ERROR: imin-bot service is missing in $COMPOSE_DIR/docker-compose.yml'
    echo 'Deploy imin-backend first (it owns the docker-compose.yml).'
    exit 1
  fi
  if ! grep -q 'IMIN_BOT_PG_PASSWORD=' .env; then
    echo 'WARNING: IMIN_BOT_PG_PASSWORD missing in .env — bot will fail to start.'
    echo 'Add it to /opt/imin/.env before bringing the service up.'
  fi
"

echo "[3/4] Building image..."
ssh -p "$SSH_PORT" "$USER@$SERVER" "
  cd $COMPOSE_DIR
  docker compose build imin-bot
  docker builder prune -af --filter 'until=24h' >/dev/null 2>&1 || true
"

echo "[4/4] Restarting service..."
ssh -p "$SSH_PORT" "$USER@$SERVER" "
  cd $COMPOSE_DIR
  docker compose up -d imin-bot
  sleep 5
  docker ps --filter name=imin-bot --format 'table {{.Names}}\t{{.Status}}'
"

echo ""
echo "=== Deploy complete ==="
echo "Logs:   ssh $USER@$SERVER 'docker logs imin-bot --tail 100 -f'"
echo "Status: ssh $USER@$SERVER 'docker ps --filter name=imin-bot'"
