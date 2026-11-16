#!/usr/bin/env bash
# Однократная подготовка сервера в РФ (Ubuntu 22.04/24.04). Запуск на сервере от root:
#   bash server_setup.sh
set -euo pipefail

# Docker из репозитория Ubuntu: download.docker.com из РФ доступен не всегда
apt-get update
apt-get install -y docker.io rsync
apt-get install -y docker-compose-v2 || apt-get install -y docker-compose-plugin

# Docker Hub ограничивает доступ из РФ — тянем базовые образы через зеркала
if [ ! -f /etc/docker/daemon.json ]; then
  cat > /etc/docker/daemon.json <<'EOF'
{
  "registry-mirrors": ["https://mirror.gcr.io", "https://dockerhub.timeweb.cloud"]
}
EOF
  systemctl restart docker
fi
systemctl enable --now docker

mkdir -p /opt/contentops
docker --version && docker compose version
echo "Готово. Теперь с локальной машины: scripts/deploy.sh root@<IP сервера>"
