#!/usr/bin/env bash
# Деплой mini-board на боевой хост. Трогает ТОЛЬКО свои артефакты (префикс mini-board).
# Запускать на боксе: /opt/mini-board/repo/deploy/redeploy.sh [ветка]
set -euo pipefail

BRANCH="${1:-main}"
ROOT=/opt/mini-board
REPO=$ROOT/repo
LOG=$ROOT/logs/deploy-journal.log

mkdir -p "$ROOT/logs" "$ROOT/data"

log() { echo "$(date -Is) $*" | tee -a "$LOG"; }

# --- снимок чужого состояния ДО: сверим после, чтобы поймать «я сломал соседа» ---
BEFORE_CONTAINERS=$(docker ps --format '{{.Names}}' | sort | tr '\n' ' ')
BEFORE_CRON=$(crontab -l 2>/dev/null | wc -l)

log "deploy start branch=$BRANCH"

cd "$REPO"
git fetch --prune origin
git checkout "$BRANCH"
git reset --hard "origin/$BRANCH"
SHA=$(git rev-parse --short HEAD)
IMAGE="mini-board:$SHA"

log "building $IMAGE"
docker build -q -t "$IMAGE" "$REPO"

# Образ прописывается в env, юнит читает ${MINIBOARD_IMAGE} — так тег меняется
# без правки systemd-юнита.
if grep -q '^MINIBOARD_IMAGE=' "$ROOT/env"; then
  sed -i "s|^MINIBOARD_IMAGE=.*|MINIBOARD_IMAGE=$IMAGE|" "$ROOT/env"
else
  echo "MINIBOARD_IMAGE=$IMAGE" >> "$ROOT/env"
fi

sudo cp "$REPO/deploy/mini-board.service" /etc/systemd/system/mini-board.service
sudo cp "$REPO/deploy/mini-board-tunnel.service" /etc/systemd/system/mini-board-tunnel.service
sudo systemctl daemon-reload
sudo systemctl restart mini-board.service          # ТОЛЬКО свой юнит
sudo systemctl enable  mini-board.service >/dev/null

# --- healthcheck по факту, а не по виду: реальный HTTP-ответ приложения ---
ok=0
for i in $(seq 1 30); do
  if curl -fsS --max-time 3 http://127.0.0.1:8088/healthz >/dev/null 2>&1; then ok=1; break; fi
  sleep 2
done
if [ "$ok" != 1 ]; then
  log "FAIL: /healthz не ответил за 60с, откат не делаю — смотреть journalctl -u mini-board"
  sudo systemctl status mini-board.service --no-pager | tail -20
  exit 1
fi

# Переменные реально доехали ДО контейнера, а не только до docker-клиента
if ! docker inspect mini-board-app --format '{{json .Config.Env}}' | grep -q MINIBOARD_BASE_URL; then
  log "FAIL: MINIBOARD_BASE_URL не проброшен в контейнер (грабля EnvironmentFile)"
  exit 1
fi

# --- «я ничего не сломал» ---
# Имена соседних сервисов специфичны для хоста и в репозиторий не попадают:
# /opt/mini-board/neighbors — две строки, NEIGHBOR_CONTAINERS= и NEIGHBOR_UNITS=,
# каждая со списком имён через пробел. Нет файла — проверка просто пропускается.
NEIGHBOR_CONTAINERS=""
NEIGHBOR_UNITS=""
[ -f "$ROOT/neighbors" ] && . "$ROOT/neighbors"

AFTER_CONTAINERS=$(docker ps --format '{{.Names}}' | sort | tr '\n' ' ')
for c in $NEIGHBOR_CONTAINERS; do
  case " $BEFORE_CONTAINERS " in
    *" $c "*) case " $AFTER_CONTAINERS " in
                *" $c "*) : ;;
                *) log "ALARM: чужой контейнер $c пропал после деплоя!"; exit 1 ;;
              esac ;;
  esac
done
for u in $NEIGHBOR_UNITS; do
  state=$(systemctl is-active "$u" 2>/dev/null || true)
  [ "$state" = active ] || log "WARN: чужой юнит $u = $state (проверить, был ли он активен до)"
done
AFTER_CRON=$(crontab -l 2>/dev/null | wc -l)
[ "$AFTER_CRON" -ge "$BEFORE_CRON" ] || log "ALARM: строк в crontab стало меньше ($BEFORE_CRON -> $AFTER_CRON)"

# --- чистка своих старых образов (диск на хосте общий) ---
docker images 'mini-board' --format '{{.Repository}}:{{.Tag}}' \
  | grep -v ":$SHA\$" | xargs -r docker rmi >/dev/null 2>&1 || true

log "deploy ok sha=$SHA image=$IMAGE disk=$(df -h / | awk 'NR==2{print $5}')"
