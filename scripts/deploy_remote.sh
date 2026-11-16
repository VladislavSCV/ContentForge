#!/usr/bin/env bash
# Перенос бота на ваш сервер одной командой, из папки проекта в WSL:
#
#   bash scripts/deploy_remote.sh --check     # только посмотреть сервер и показать план, ничего не менять
#   bash scripts/deploy_remote.sh             # то же + после вашего «yes» выложить и запустить
#   bash scripts/deploy_remote.sh root@1.2.3.4   # другой сервер (по умолчанию DEPLOY_HOST из .env)
#
# Что делает, по шагам:
#   1. Подключается к серверу с ЖЁСТКОЙ проверкой ключа хоста (known_hosts). Если отпечаток не совпадёт, остановится.
#      Берёт только ~/.ssh/id_ed25519 и ~/.ssh/id_rsa; другие ключи (например, .pem для других задач) не использует.
#   2. Только читает: система, диск, память, Docker, что уже запущено. Ничего не меняет.
#   3. Показывает план и ждёт «yes». Без него ничего не выкладывается.
#   4. Ставит Docker, ТОЛЬКО если его нет. Чужие контейнеры, настройки Docker, файрвол и sshd не трогает.
#   5. Копирует проект и .env в /opt/contentops (права .env: 600). Если там лежит чужое, остановится.
#   6. Останавливает бота на этом компьютере (иначе два бота с одним токеном мешают друг другу) и запускает на сервере.
#      Если что-то пошло не так, сам вернёт бота на этот компьютер.
#
# Секреты (.env) уходят только по SSH на ваш сервер и нигде не печатаются.
set -euo pipefail

HOST=""
DIR="/opt/contentops"
CHECK_ONLY=0
for a in "$@"; do
  case "$a" in
    --check) CHECK_ONLY=1 ;;
    *@*) HOST="$a" ;;
  esac
done

cd "$(dirname "$0")/.."

# адрес сервера — личные данные, в репозитории его нет: берём из DEPLOY_HOST в .env (весь .env не читаем и не подключаем)
if [ -z "$HOST" ] && [ -f .env ]; then
  HOST="$(grep -E '^DEPLOY_HOST=' .env | tail -1 | cut -d= -f2- | tr -d '"' || true)"
fi
if [ -z "$HOST" ]; then
  echo "Не задан сервер: добавьте DEPLOY_HOST=root@адрес в .env или передайте root@адрес аргументом." >&2
  exit 1
fi

# Журнал: весь вывод скрипта (и ошибки) дублируется в файл с временем запуска, отдельно журнал SSH.
# Секретов в них нет: скрипт их не печатает, а в журнале SSH только служебные строки соединения.
umask 077
LOGDIR="data/deploy_logs"; mkdir -p "$LOGDIR"
LOG="$LOGDIR/deploy_$(date +%Y%m%d_%H%M%S).log"
SSHLOG="${LOG%.log}.ssh.log"
exec > >(tee -a "$LOG") 2>&1
echo "Журнал: $LOG (SSH: $SSHLOG)"

# одно SSH-соединение на весь запуск (ControlMaster): десяток отдельных подключений подряд сервер может счесть переборкой
# и временно закрыть порт 22 (fail2ban, лимиты). Плюс повторные попытки и keepalive против коротких провалов сети.
CM="$HOME/.ssh/cm-contentops-%C"
SSH=(ssh -o StrictHostKeyChecking=yes -o BatchMode=yes -o ConnectTimeout=20 -o ConnectionAttempts=3
     -o ServerAliveInterval=15 -o ServerAliveCountMax=4
     -o ControlMaster=auto -o ControlPath="$CM" -o ControlPersist=10m
     -v -E "$SSHLOG"
     -o IdentitiesOnly=yes -i "$HOME/.ssh/id_ed25519" -i "$HOME/.ssh/id_rsa")
IP="${HOST#*@}"
trap 'rm -f app/_version.txt; "${SSH[@]}" -O exit "$HOST" >/dev/null 2>&1 || true' EXIT   # убрать файл версии, закрыть общее соединение
LOCAL_STOPPED=0

say() { printf '\n== [%s] %s\n' "$(date +%T)" "$*"; }
die() { printf '\nСТОП: %s\n' "$*" >&2; echo "Журнал: $LOG"; exit 1; }

# что с сетью до сервера: помогает отличить «сервер недоступен» от «мой адрес заблокирован» и «мешает VPN»
diagnose() {
  echo "-- Диагностика сети до $IP"
  if ping -c 2 -W 3 "$IP" >/dev/null 2>&1; then echo "   ping: отвечает"; else echo "   ping: нет ответа (или ICMP закрыт)"; fi
  if timeout 6 bash -c "exec 3<>/dev/tcp/$IP/22" 2>/dev/null; then echo "   порт 22: открывается"
  else echo "   порт 22: НЕ открывается (сервер выключен, адрес заблокирован, мешает VPN или файрвол)"; fi
  ip route get "$IP" 2>/dev/null | head -1 | sed 's/^/   маршрут: /' || true
}

# любая ошибка: номер строки, команда, код, диагностика сети (код 255 у ssh = не удалось подключиться), откат
on_error() {
  local code="$1" line="$2" cmd="$3"
  echo
  echo "ОШИБКА (код $code) на строке $line: $cmd"
  if [ "$code" = 255 ]; then diagnose; fi
  echo "Подробности: $LOG ; журнал SSH: $SSHLOG"
  rollback
}

# если что-то упадёт после остановки локального бота, возвращаем его, чтобы не остаться совсем без бота
rollback() {
  if [ "$LOCAL_STOPPED" = 1 ]; then
    echo "Что-то пошло не так. Возвращаю бота на этот компьютер…"
    docker compose start >/dev/null 2>&1 && echo "Локальный бот снова запущен." || echo "Не вышло, запусти вручную: docker compose up -d"
  fi
}
trap 'on_error $? $LINENO "$BASH_COMMAND"' ERR   # BASH_COMMAND здесь — упавшая команда, а не внутренность функции

[ -f .env ] || die "нет файла .env в папке проекта"
command -v tar >/dev/null || die "нет tar"
[ -f "$HOME/.ssh/id_ed25519" ] || [ -f "$HOME/.ssh/id_rsa" ] || die "нет ключей ~/.ssh/id_ed25519 или id_rsa"

say "1. Проверка ключа сервера"
ssh-keygen -F "$IP" -l 2>/dev/null | grep -v '^#' | head -3 || die "сервер $IP не найден в ~/.ssh/known_hosts: сверь отпечаток с панелью хостинга и подключись один раз вручную: ssh $HOST"
echo "(подключение пойдёт только если ключ сервера совпадёт с записью выше)"

say "2. Осмотр сервера (только чтение)"
PROBE="$("${SSH[@]}" "$HOST" 'bash -s' <<'REMOTE'
echo "WHO=$(whoami)@$(hostname)"
echo "OS=$(grep -E '^PRETTY_NAME=' /etc/os-release | cut -d= -f2 | tr -d '"')"
echo "DISK_FREE_GB=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')"
echo "MEM_MB=$(free -m | awk 'NR==2{print $2}')"
echo "DOCKER=$(command -v docker >/dev/null && docker --version | cut -d, -f1 || echo нет)"
echo "COMPOSE=$(docker compose version 2>/dev/null | head -1 || echo нет)"
echo "CONTAINERS=$(docker ps -q 2>/dev/null | wc -l)"
echo "CONTAINER_NAMES=$(docker ps --format '{{.Names}}' 2>/dev/null | head -10 | tr '\n' ' ')"
echo "PORTS=$(ss -tlnH 2>/dev/null | awk '{print $4}' | sed 's/.*://' | sort -un | head -15 | tr '\n' ' ')"
if [ -e /opt/contentops ]; then
  if [ -f /opt/contentops/docker-compose.yml ] && [ -d /opt/contentops/app ]; then echo "TARGET=наша установка (обновим)";
  elif [ -z "$(ls -A /opt/contentops)" ]; then echo "TARGET=пустая папка";
  else echo "TARGET=ЗАНЯТО ЧУЖИМ"; fi
else echo "TARGET=нет папки (создадим)"; fi
echo "DB_REMOTE=$([ -f /opt/contentops/data/contentops.sqlite3 ] && echo есть || echo нет)"
REMOTE
)" || { diagnose; die "не удалось подключиться по SSH. Проверь ключ и адрес. Подробности в журнале SSH: $SSHLOG"; }
echo "$PROBE" | sed 's/^/   /'

val() { echo "$PROBE" | grep "^$1=" | head -1 | cut -d= -f2-; }
FREE="$(val DISK_FREE_GB)"; MEM="$(val MEM_MB)"; TARGET="$(val TARGET)"
HAS_DOCKER=0; [ "$(val DOCKER)" != "нет" ] && HAS_DOCKER=1
HAS_COMPOSE=0; [ "$(val COMPOSE)" != "нет" ] && [ -n "$(val COMPOSE)" ] && HAS_COMPOSE=1

PROBLEMS=()
[ "${FREE:-0}" -ge 6 ] || PROBLEMS+=("мало места на диске: ${FREE:-?} ГБ, для образа нужно от 6 ГБ")
[ "${MEM:-0}" -ge 1500 ] || PROBLEMS+=("мало памяти: ${MEM:-?} МБ, рендер видео с браузером требует от 1,5 ГБ")
[ "$TARGET" != "ЗАНЯТО ЧУЖИМ" ] || PROBLEMS+=("папка $DIR занята чужими файлами")
if [ "$HAS_DOCKER" = 1 ] && [ "$HAS_COMPOSE" = 0 ]; then PROBLEMS+=("Docker есть, а плагина compose нет: поставь его сам, я не буду менять существующий Docker"); fi

say "3. План"
echo "   Сервер:      $HOST"
echo "   Папка:       $DIR ($TARGET)"
[ "$HAS_DOCKER" = 1 ] && echo "   Docker:      уже есть, не трогаем" || echo "   Docker:      нет, будет установлен docker.io и docker-compose-v2 из репозитория Ubuntu"
echo "   Копируется:  код проекта и .env (права 600); без data/, .git, ключей client_secret*.json, выгрузок аналитики"
[ "$(val DB_REMOTE)" = "нет" ] && [ -f data/contentops.sqlite3 ] && echo "   База:        локальная data/contentops.sqlite3 копируется один раз (на сервере её нет)" \
  || echo "   База:        на сервере остаётся как есть"
echo "   Локальный бот: будет остановлен (docker compose stop), на сервере запущен; при сбое вернётся сам"
echo "   Не трогаем:  чужие контейнеры, файрвол, sshd, настройки Docker, пользователей"
if [ "${#PROBLEMS[@]}" -gt 0 ]; then
  printf '\nПРОБЛЕМЫ, из-за которых я не продолжу:\n'; printf '   - %s\n' "${PROBLEMS[@]}"
  exit 1
fi
[ "$CHECK_ONLY" = 1 ] && { echo; echo "Режим --check: на этом всё. Ничего не изменено."; exit 0; }

printf '\nВыкладываем? Напиши yes и Enter (всё остальное отменит): '
read -r ANSWER </dev/tty
[ "$ANSWER" = "yes" ] || { echo "Отменено. Ничего не изменено."; exit 0; }

if [ "$HAS_DOCKER" = 0 ]; then
  say "4. Установка Docker"
  "${SSH[@]}" "$HOST" 'export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get install -y -qq docker.io rsync && (apt-get install -y -qq docker-compose-v2 || apt-get install -y -qq docker-compose-plugin) && systemctl enable --now docker && docker --version && docker compose version'
fi

say "5. Копирование проекта"
"${SSH[@]}" "$HOST" "mkdir -p $DIR/data && chmod 700 $DIR"
# версия выкладки: дата + короткий отпечаток содержимого (код, подсказки, словарь, ресурсы). Бот показывает её в /version,
# и по ней видно, дошло ли обновление. Файл лежит только в выкладываемой копии: после копирования удаляем локальный.
VERSION_DIGEST="$(find app assets scripts cloudflare Dockerfile docker-compose.yml requirements.txt -type f \
    ! -path '*/__pycache__/*' ! -name '_version.txt' -print0 2>/dev/null | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -c1-8)"
printf '%s|%s|%s\n' "$(date '+%d.%m %H:%M')" "$VERSION_DIGEST" "$IP" > app/_version.txt
tar czf - --exclude='./.git' --exclude='./data' --exclude='__pycache__' --exclude='./.venv' --exclude='./node_modules' \
    --exclude='client_secret*.json' --exclude='PostPerformance*' --exclude='*.Zone.Identifier' \
    --exclude='./.wrangler' --exclude='./.dev.vars' . \
  | "${SSH[@]}" "$HOST" "tar xzf - -C $DIR && chmod 600 $DIR/.env"
rm -f app/_version.txt
echo "   скопировано, версия $VERSION_DIGEST"

say "6. Остановка локального бота"
if docker compose ps --status running 2>/dev/null | grep -q contentops; then
  docker compose stop
  LOCAL_STOPPED=1
else
  echo "   локальный бот не запущен, пропускаю"
fi

if [ "$(val DB_REMOTE)" = "нет" ] && [ -f data/contentops.sqlite3 ]; then
  say "   Копирую базу (бот остановлен, файл целый)"
  "${SSH[@]}" "$HOST" "cat > $DIR/data/contentops.sqlite3 && chmod 600 $DIR/data/contentops.sqlite3" < data/contentops.sqlite3
fi

say "7. Запуск на сервере (сборка образа занимает несколько минут)"
"${SSH[@]}" "$HOST" "cd $DIR && docker compose up -d --build"
sleep 25

say "8. Проверка"
"${SSH[@]}" "$HOST" "cd $DIR && docker compose ps && echo && docker inspect -f 'состояние={{.State.Status}} перезапусков={{.RestartCount}}' \$(docker compose ps -q contentops)"
echo
echo "Последние строки журнала (токены скрыты):"
"${SSH[@]}" "$HOST" "cd $DIR && docker compose logs --tail 15 2>&1" | sed -E 's/bot[0-9]+:[A-Za-z0-9_-]+/bot<скрыто>/g; s/[A-Za-z0-9_-]{35,}/<скрыто>/g'

if "${SSH[@]}" "$HOST" "cd $DIR && docker compose logs 2>&1 | grep -q 'ContentOps started'"; then
  LOCAL_STOPPED=0   # сервер работает, возвращать локального бота не нужно
  echo
  echo "ГОТОВО. Бот работает на сервере. В Telegram должно прийти «🟢 Бот запущен (версия $VERSION_DIGEST)». Проверь командой /version: отпечаток должен совпасть."
  echo "Локальный бот остановлен. Если нужно вернуть: docker compose up -d (но не одновременно с серверным)."
else
  false   # сработает rollback: локальный бот вернётся
fi
