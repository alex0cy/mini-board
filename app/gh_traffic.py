"""Снимок GitHub-трафика репозитория → уровень воронки «узнал».

Запускается по cron раз в сутки. GitHub отдаёт traffic только за последние
14 дней и только владельцу репо, поэтому снапшоты надо копить у себя —
пропустишь две недели, данные потеряны навсегда.

Запуск: python -m app.gh_traffic
Нужен MINIBOARD_GH_REPO=owner/name и токен в MINIBOARD_GH_TOKEN_FILE.
"""
import json
import os
import sys
import urllib.error
import urllib.request

from . import db

REPO = os.environ.get("MINIBOARD_GH_REPO", "alex0cy/mini-board")
TOKEN_FILE = os.environ.get("MINIBOARD_GH_TOKEN_FILE", "/opt/mini-board/gh-token")


def _get(path: str, token: str):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}{path}",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "mini-board-traffic"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main() -> int:
    with open(TOKEN_FILE) as fh:
        token = fh.read().strip()

    try:
        views = _get("/traffic/views", token)
        clones = _get("/traffic/clones", token)
        refs = _get("/traffic/popular/referrers", token)
        repo = _get("", token)
    except urllib.error.HTTPError as e:
        print(f"github api {e.code}: {e.reason}", file=sys.stderr)
        return 1

    by_day = {}
    for item in views.get("views", []):
        d = item["timestamp"][:10]
        by_day.setdefault(d, {})["views"] = item["count"]
        by_day[d]["unique_views"] = item["uniques"]
    for item in clones.get("clones", []):
        d = item["timestamp"][:10]
        by_day.setdefault(d, {})["clones"] = item["count"]
        by_day[d]["unique_clones"] = item["uniques"]

    ref_json = json.dumps(refs, ensure_ascii=False)
    db.init()
    with db.tx() as conn:
        for day, v in by_day.items():
            conn.execute(
                "INSERT INTO github_traffic (day, views, unique_views, clones, unique_clones,"
                " stars, forks, referrers) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(day) DO UPDATE SET views=excluded.views,"
                "  unique_views=excluded.unique_views, clones=excluded.clones,"
                "  unique_clones=excluded.unique_clones, stars=excluded.stars,"
                "  forks=excluded.forks, referrers=excluded.referrers",
                (day, v.get("views", 0), v.get("unique_views", 0), v.get("clones", 0),
                 v.get("unique_clones", 0), repo.get("stargazers_count", 0),
                 repo.get("forks_count", 0), ref_json))
    print(f"github traffic: {len(by_day)} дней обновлено, "
          f"stars={repo.get('stargazers_count')} forks={repo.get('forks_count')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
