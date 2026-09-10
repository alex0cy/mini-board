# Деплой mini-board на боевой хост

Хост общий с другими долгоживущими сервисами, поэтому всё именуется с префиксом
`mini-board` и чужие артефакты не трогаются. Здесь только последовательность шагов;
ограничения конкретного хоста — во внутреннем `AGENTS.md`, он вне репозитория.

Ниже `$BOARD_HOST` — это `user@host` вашего сервера.

Принцип: **входящих портов не открываем**. Приложение слушает `127.0.0.1:8088`,
наружу его публикует Cloudflare-туннель исходящим соединением. Файрвол хоста
не трогаем вообще.

## Один раз, руками (человеком)

1. **Fine-grained PAT** на репозиторий `mini-board`: Contents — Read and write,
   плюс **Administration — Read** (без неё `/traffic/*` отдаёт 403, и уровень
   воронки «узнал» останется пустым). Токен → `~/.gh-token-mini-board`, chmod 600.
2. **Cloudflare**: ничего в дашборде делать не нужно, достаточно чтобы зона
   `alex.eu.org` была активна. Туннель создаётся с CLI на боксе (см. ниже) —
   Zero Trust-дашборд и привязка карты не требуются.

## Первичная установка на боксе

```bash
ssh -i ~/.ssh/contabo.key $BOARD_HOST

sudo mkdir -p /opt/mini-board && sudo chown opc:opc /opt/mini-board
mkdir -p /opt/mini-board/{data,logs}

# ВАЖНО: клонировать БЕЗ umask 177 — под ним git создаёт .git/objects
# без x-бита и клон падает с Permission denied (грабля, собранная на этом хосте).
git clone https://github.com/OWNER/mini-board.git /opt/mini-board/repo

# секреты, все 600
cp /opt/mini-board/repo/deploy/env.example /opt/mini-board/env
chmod 600 /opt/mini-board/env
$EDITOR /opt/mini-board/env          # заполнить BASE_URL, SALT, GH_REPO

printf '%s\n' 'ghp_...' > /opt/mini-board/gh-token && chmod 600 /opt/mini-board/gh-token

# cloudflared
sudo curl -fsSL -o /usr/local/bin/cloudflared \
  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
sudo chmod +x /usr/local/bin/cloudflared

# --- туннель, локально управляемый: дашборд Zero Trust не нужен ---
# Проверить, что бокс вообще может достучаться до Cloudflare:
nc -zv region1.v2.argotunnel.com 7844

export CLOUDFLARED_HOME=/opt/mini-board/cloudflared
mkdir -p "$CLOUDFLARED_HOME"

# login откроет ссылку — открыть её в браузере и выбрать зону alex.eu.org.
# Положит cert.pem (живёт 10+ лет, это ключ от аккаунта — беречь как секрет).
cloudflared --origincert "$CLOUDFLARED_HOME/cert.pem" tunnel login

cloudflared --origincert "$CLOUDFLARED_HOME/cert.pem" \
  tunnel create --credentials-file "$CLOUDFLARED_HOME/mini-board.json" mini-board

cp /opt/mini-board/repo/deploy/cloudflared-config.example.yml "$CLOUDFLARED_HOME/config.yml"
$EDITOR "$CLOUDFLARED_HOME/config.yml"      # вписать UUID и путь к mini-board.json

# заведёт CNAME board.alex.eu.org -> <UUID>.cfargotunnel.com прямо в зоне
cloudflared --origincert "$CLOUDFLARED_HOME/cert.pem" \
  tunnel route dns mini-board board.alex.eu.org

chmod 600 "$CLOUDFLARED_HOME"/cert.pem "$CLOUDFLARED_HOME"/*.json

/opt/mini-board/repo/deploy/redeploy.sh main
sudo systemctl enable --now mini-board-tunnel.service
/opt/mini-board/repo/deploy/install-cron.sh
```

## Обновление

```bash
/opt/mini-board/repo/deploy/redeploy.sh main
```

`redeploy.sh` сам проверяет по факту, а не по виду: реальный ответ `/healthz`,
реальный `docker inspect` на проброс переменных, живы ли чужие контейнеры и
юниты, не убавилось ли строк в crontab. При провале — падает и пишет в
`/opt/mini-board/logs/deploy-journal.log`.

## Проверка «я ничего не сломал»

```bash
docker ps --format '{{.Names}}'    # соседние контейнеры всё ещё Up
systemctl is-active $NEIGHBOR_UNITS # см. /opt/mini-board/neighbors
crontab -l | wc -l                 # +3 своих строки с маркером '# mini-board', чужие на месте
df -h / | tail -1
```

## Дашборд без публикации наружу

```bash
ssh -i ~/.ssh/contabo.key -L 8088:127.0.0.1:8088 $BOARD_HOST
# затем http://127.0.0.1:8088/dashboard в браузере
```

## Откат

```bash
docker images mini-board            # найти предыдущий sha
sed -i 's|^MINIBOARD_IMAGE=.*|MINIBOARD_IMAGE=mini-board:<старый-sha>|' /opt/mini-board/env
sudo systemctl restart mini-board.service
```

БД переживает откат: она в `/opt/mini-board/data`, а не в образе.
