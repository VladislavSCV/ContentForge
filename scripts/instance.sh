#!/usr/bin/env bash
# Инстансы команд (по одному на команду, общий код и образ).
#   scripts/instance.sh new  <команда> [профиль]   создать instances/<команда>/ из шаблона
#   scripts/instance.sh up|down|logs|ps|config <команда>
# Всё локально: на сервер ничего не отправляется, деплой делается отдельно и вручную.
set -euo pipefail
cd "$(dirname "$0")/.."

cmd="${1:?usage: scripts/instance.sh new|up|down|logs|ps|config <команда> [профиль]}"
team="${2:?укажи имя команды (латиница, цифры, дефис)}"
[[ "$team" =~ ^[a-z0-9][a-z0-9-]*$ && "$team" != _* ]] || { echo "имя команды: a-z, 0-9, дефис; без _ в начале"; exit 1; }
dir="instances/$team"
compose() { INSTANCE="$team" docker compose -p "cf-$team" -f docker-compose.instance.yml "$@"; }

case "$cmd" in
  new)
    profile="${3:-general}"
    [ -f "profiles/$profile/profile.toml" ] || { echo "нет профиля profiles/$profile; есть: $(ls profiles | tr '\n' ' ')"; exit 1; }
    [ ! -e "$dir" ] || { echo "$dir уже есть, ничего не трогаю"; exit 1; }
    mkdir -p "$dir"/{data,backgrounds,mascot}
    sed "s/^PROFILE=.*/PROFILE=$profile/" instances/_template/.env.example > "$dir/.env"
    chmod 600 "$dir/.env"
    echo "создано $dir: заполни $dir/.env (токены, канал, бренд); фоны — $dir/backgrounds, маскот — $dir/mascot/mascot.png"
    ;;
  up)     [ -f "$dir/.env" ] || { echo "нет $dir/.env: сначала new"; exit 1; }; compose up -d --build ;;
  down)   compose down ;;
  logs)   compose logs -f --tail=100 ;;
  ps)     compose ps ;;
  config) compose config ;;
  *) echo "неизвестная команда $cmd"; exit 1 ;;
esac
