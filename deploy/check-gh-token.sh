#!/usr/bin/env bash
# Проверяет, что PAT реально даёт доступ к Traffic API, а не только выглядит рабочим.
# Запускать после каждой ротации токена: /opt/mini-board/repo/deploy/check-gh-token.sh
set -euo pipefail

ROOT=/opt/mini-board
TOKEN_FILE="${MINIBOARD_GH_TOKEN_FILE:-$ROOT/gh-token}"
REPO="${MINIBOARD_GH_REPO:-alex0cy/mini-board}"

[ -f "$TOKEN_FILE" ] || { echo "нет файла токена: $TOKEN_FILE"; exit 1; }
TOKEN=$(cat "$TOKEN_FILE")

hdr=$(mktemp); body=$(mktemp)
trap 'rm -f "$hdr" "$body"' EXIT

check() {
  local path="$1" label="$2"
  local code
  code=$(curl -s -D "$hdr" -o "$body" -w '%{http_code}' \
    -H "Authorization: Bearer $TOKEN" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/$REPO$path")

  if [ "$code" = 200 ]; then
    echo "OK   $label"
    return 0
  fi

  echo "FAIL $label — HTTP $code"
  # GitHub прямо называет недостающее право в этом заголовке — не гадаем
  grep -i '^x-accepted-github-permissions:' "$hdr" || true
  head -c 200 "$body"; echo
  return 1
}

rc=0
check ""                          "репозиторий виден (Metadata)"            || rc=1
check "/traffic/views"            "traffic/views (нужно Administration:Read)"   || rc=1
check "/traffic/clones"           "traffic/clones (нужно Administration:Read)"  || rc=1
check "/traffic/popular/referrers" "referrers (нужно Administration:Read)"      || rc=1

if [ "$rc" = 0 ]; then
  echo
  echo "Токен годен: воронка сможет наполнять уровень «узнал»."
else
  echo
  echo "Traffic API недоступен. Заголовок x-accepted-github-permissions выше"
  echo "называет точное недостающее право. Чаще всего это Administration: Read."
fi
exit $rc
