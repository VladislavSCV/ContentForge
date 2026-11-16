#!/usr/bin/env bash
# Сравнивает поведение текущего кода со старым коммитом: ./tests/behavior_check.sh [коммит]  (по умолчанию c91b940, до рефакторинга)
# Нужен .venv с зависимостями. Старый код берётся из git archive, новый из рабочей папки.
set -euo pipefail
cd "$(dirname "$0")/.."
BASE=${1:-c91b940}; B=$(mktemp -d); trap 'rm -rf "$B"' EXIT
git archive "$BASE" app | tar -x -C "$B"
ln -s "$PWD/assets" "$B/assets"
export BRAND_NAME=TestBrand BRAND_HANDLE=@t TG_CHANNEL_LINK=https://t.me/t TELEGRAM_BOT_TOKEN=x ADMIN_IDS=1
PY="$PWD/.venv/bin/python"; rc=0
for p in engineering general; do
  export CONTENT_PROFILE=$p
  # старый код не знал про ленты профиля: даём путь явно, чтобы условия были одинаковыми
  if [ "$p" = general ]; then export SOURCES_FILE=assets/sources_general.txt; else export SOURCES_FILE=assets/sources.txt; fi
  (cd "$B" && "$PY" "$OLDPWD/tests/behavior_snapshot.py" 2>/dev/null > "$B/old_$p.json")
  "$PY" tests/behavior_snapshot.py 2>/dev/null > "$B/new_$p.json"
  if cmp -s "$B/old_$p.json" "$B/new_$p.json"; then echo "[$p] поведение совпадает с $BASE ($(wc -c < "$B/new_$p.json") байт)"
  else echo "[$p] ОТЛИЧИЯ"; diff "$B/old_$p.json" "$B/new_$p.json" | head -20; rc=1; fi
done
exit $rc
