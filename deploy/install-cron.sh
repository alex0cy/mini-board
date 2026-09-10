#!/usr/bin/env bash
# Идемпотентная установка cron-строк mini-board. Чужие строки НЕ трогает:
# читает текущий crontab, добавляет только недостающие свои (маркер '# mini-board').
set -euo pipefail

ROOT=/opt/mini-board
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

crontab -l 2>/dev/null > "$TMP" || true
BEFORE=$(wc -l < "$TMP")

add() {
  grep -qF "$1" "$TMP" || echo "$1" >> "$TMP"
}

# Снимок GitHub-трафика раз в сутки (уровень воронки «узнал»).
# GitHub хранит traffic только 14 дней — пропуск = безвозвратная дыра в данных.
# Токен монтируется ro по ТОМУ ЖЕ пути, что на хосте: тогда MINIBOARD_GH_TOKEN_FILE
# из env корректен и на хосте (check-gh-token.sh), и внутри контейнера.
# Без этого монтирования задача падает с FileNotFoundError — молча, раз в сутки.
add "17 3 * * * docker run --rm --env-file $ROOT/env -v $ROOT/data:/data -v $ROOT/gh-token:$ROOT/gh-token:ro \$(grep '^MINIBOARD_IMAGE=' $ROOT/env | cut -d= -f2) python -m app.gh_traffic >> $ROOT/logs/traffic.log 2>&1 # mini-board"

# Дневной отчёт по воронке в лог (чтобы история была даже если БД потеряется)
add "22 3 * * * curl -fsS --max-time 10 http://127.0.0.1:8088/v1/stats?days=1 >> $ROOT/logs/stats-daily.jsonl 2>&1 && echo >> $ROOT/logs/stats-daily.jsonl # mini-board"

# Healthcheck своего юнита каждые 30 минут
add "*/30 * * * * curl -fsS --max-time 5 http://127.0.0.1:8088/healthz >/dev/null || echo \"\$(date -Is) miniboard healthz FAIL\" >> $ROOT/logs/deploy-journal.log # mini-board"

AFTER=$(wc -l < "$TMP")
crontab "$TMP"
echo "crontab: было $BEFORE строк, стало $AFTER (добавлено $((AFTER-BEFORE)) своих)"
