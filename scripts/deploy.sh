#!/usr/bin/env bash
# Выкладка бота на сервер: scripts/deploy.sh root@1.2.3.4 [/opt/contentops]
# Копирует код и .env, пересобирает и перезапускает контейнер. Данные (data/) на сервере не трогает.
set -euo pipefail

TARGET="${1:?usage: scripts/deploy.sh user@host [dir]}"
DIR="${2:-/opt/contentops}"
cd "$(dirname "$0")/.."

[ -f .env ] || { echo "нет .env"; exit 1; }

rsync -az --delete \
  --exclude '.venv/' --exclude '__pycache__/' --exclude 'data/' --exclude '.git/' \
  --exclude 'client_secret*.json' --exclude '.wrangler/' --exclude '.dev.vars' \
  ./ "$TARGET:$DIR/"

ssh "$TARGET" "cd $DIR && mkdir -p data && docker compose up -d --build && docker compose ps && docker compose logs --tail 20"
