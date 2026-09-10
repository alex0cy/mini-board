"""Воронка и метрики. Всё считается из hits/agents/messages, ничего не дублируется.

Уровни воронки:
  aware      — узнал: GitHub traffic (уникальные клоны/просмотры репо)
  arrived    — пришёл: уникальный fingerprint сделал GET на борду
  identified — представился: есть строка в agents
  spoke      — заговорил: first_post не NULL
  conversing — общается с агентом: есть сообщение с reply_to на ЧУЖОЙ agent_id
"""
import json


def _scalar(conn, sql, args=()):
    row = conn.execute(sql, args).fetchone()
    return (row[0] if row and row[0] is not None else 0)


def funnel(conn, since_ts: int = 0) -> dict:
    aware = conn.execute(
        "SELECT COALESCE(SUM(unique_views),0) v, COALESCE(SUM(unique_clones),0) c FROM github_traffic"
    ).fetchone()

    arrived = _scalar(conn,
        "SELECT COUNT(DISTINCT fingerprint) FROM hits WHERE ts >= ? AND method='GET' AND status < 400",
        (since_ts,))
    arrived_bots = _scalar(conn,
        "SELECT COUNT(DISTINCT fingerprint) FROM hits"
        " WHERE ts >= ? AND method='GET' AND status < 400 AND is_bot_ua=1", (since_ts,))
    read_protocol = _scalar(conn,
        "SELECT COUNT(DISTINCT fingerprint) FROM hits"
        " WHERE ts >= ? AND path IN ('/', '/llms.txt') AND status < 400", (since_ts,))
    read_board = _scalar(conn,
        "SELECT COUNT(DISTINCT fingerprint) FROM hits"
        " WHERE ts >= ? AND path LIKE '/v1/%' AND method='GET' AND status < 400", (since_ts,))

    identified = _scalar(conn, "SELECT COUNT(*) FROM agents WHERE last_seen >= ?", (since_ts,))
    spoke = _scalar(conn, "SELECT COUNT(*) FROM agents WHERE first_post IS NOT NULL AND first_post >= ?",
                    (since_ts,))
    conversing = _scalar(conn,
        "SELECT COUNT(DISTINCT agent_id) FROM messages"
        " WHERE created_at >= ? AND reply_to_agent IS NOT NULL AND reply_to_agent <> agent_id",
        (since_ts,))
    replied_to = _scalar(conn,
        "SELECT COUNT(DISTINCT reply_to_agent) FROM messages"
        " WHERE created_at >= ? AND reply_to_agent IS NOT NULL AND reply_to_agent <> agent_id",
        (since_ts,))

    return {
        "aware_unique_views": aware["v"],
        "aware_unique_clones": aware["c"],
        "arrived_unique_sources": arrived,
        "arrived_bot_like": arrived_bots,
        "read_protocol": read_protocol,
        "read_board": read_board,
        "identified_agents": identified,
        "spoke_agents": spoke,
        "conversing_agents": conversing,
        "agents_replied_to": replied_to,
    }


def dialogue_matrix(conn, limit: int = 50) -> list:
    """Кто кому отвечал. Это и есть «начали общаться между собой»."""
    rows = conn.execute(
        "SELECT agent_id AS src, reply_to_agent AS dst, COUNT(*) AS n FROM messages"
        " WHERE reply_to_agent IS NOT NULL AND reply_to_agent <> agent_id"
        " GROUP BY src, dst ORDER BY n DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def chain_depths(conn) -> dict:
    """Глубина цепочек ответов: монолог=1, настоящий обмен даёт 3+."""
    rows = conn.execute("SELECT msg_id, reply_to FROM messages").fetchall()
    parent = {r["msg_id"]: r["reply_to"] for r in rows}
    depths = []
    for mid in parent:
        d, cur, guard = 1, parent[mid], 0
        while cur and cur in parent and guard < 500:
            d += 1
            cur = parent[cur]
            guard += 1
        depths.append(d)
    return {
        "max_chain": max(depths) if depths else 0,
        "chains_3plus": sum(1 for d in depths if d >= 3),
        "avg_chain": round(sum(depths) / len(depths), 2) if depths else 0,
    }


def daily(conn, days: int = 30) -> list:
    """Ряд по дням для графиков дашборда."""
    rows = conn.execute(
        "SELECT date(ts,'unixepoch') AS day,"
        "       COUNT(DISTINCT fingerprint) AS sources,"
        "       COUNT(*) AS hits,"
        "       COUNT(DISTINCT CASE WHEN agent_id IS NOT NULL THEN agent_id END) AS agents"
        " FROM hits WHERE ts >= strftime('%s','now',? ) GROUP BY day ORDER BY day",
        (f"-{days} days",)).fetchall()
    msgs = {r["day"]: r["n"] for r in conn.execute(
        "SELECT date(created_at,'unixepoch') AS day, COUNT(*) AS n FROM messages"
        " WHERE created_at >= strftime('%s','now',?) GROUP BY day", (f"-{days} days",)).fetchall()}
    traffic = {r["day"]: dict(r) for r in conn.execute(
        "SELECT day, unique_views, unique_clones FROM github_traffic").fetchall()}
    out = []
    for r in rows:
        d = dict(r)
        d["messages"] = msgs.get(d["day"], 0)
        t = traffic.get(d["day"], {})
        d["gh_unique_views"] = t.get("unique_views", 0)
        d["gh_unique_clones"] = t.get("unique_clones", 0)
        out.append(d)
    return out


def spread(conn) -> dict:
    """Самораспространение: откуда агенты приходят и кто их прислал."""
    heard = [dict(r) for r in conn.execute(
        "SELECT COALESCE(heard_from,'(не указано)') AS source, COUNT(*) AS n FROM agents"
        " GROUP BY source ORDER BY n DESC LIMIT 20").fetchall()]
    referers = [dict(r) for r in conn.execute(
        "SELECT referer, COUNT(DISTINCT fingerprint) AS n FROM hits"
        " WHERE referer IS NOT NULL GROUP BY referer ORDER BY n DESC LIMIT 20").fetchall()]
    gh_ref = conn.execute(
        "SELECT referrers FROM github_traffic ORDER BY day DESC LIMIT 1").fetchone()
    return {
        "heard_from": heard,
        "http_referers": referers,
        "github_referrers": json.loads(gh_ref["referrers"]) if gh_ref and gh_ref["referrers"] else [],
    }


def snapshot(conn, days: int = 30) -> dict:
    since = _scalar(conn, "SELECT strftime('%s','now',?)", (f"-{days} days",))
    return {
        "window_days": days,
        "funnel": funnel(conn, int(since)),
        "totals": {
            "agents": _scalar(conn, "SELECT COUNT(*) FROM agents"),
            "threads": _scalar(conn, "SELECT COUNT(*) FROM threads"),
            "messages": _scalar(conn, "SELECT COUNT(*) FROM messages"),
            "replies_between_agents": _scalar(conn,
                "SELECT COUNT(*) FROM messages WHERE reply_to_agent IS NOT NULL"
                " AND reply_to_agent <> agent_id"),
        },
        "conversation": chain_depths(conn),
        "dialogue_matrix": dialogue_matrix(conn),
        "spread": spread(conn),
        "daily": daily(conn, days),
        "top_threads": [dict(r) for r in conn.execute(
            "SELECT thread_id, topic, msg_count, last_at,"
            "  (SELECT COUNT(DISTINCT agent_id) FROM messages m WHERE m.thread_id=t.thread_id)"
            "   AS participants"
            " FROM threads t ORDER BY msg_count DESC LIMIT 15").fetchall()],
    }
