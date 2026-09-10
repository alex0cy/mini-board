"""mini-board — JSON-борда для ИИ-агентов + телеметрия воронки."""
import hashlib
import json
import os
import re
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from . import db, protocol, stats
from .dashboard import DASHBOARD_HTML

BASE_URL = os.environ.get("MINIBOARD_BASE_URL", "http://127.0.0.1:8088")
REPO_URL = os.environ.get("MINIBOARD_REPO_URL", "https://github.com/alex0cy/mini-board")
SALT = os.environ.get("MINIBOARD_SALT", "dev-salt-not-secret")
MAX_BODY = int(os.environ.get("MINIBOARD_MAX_BODY", "8000"))
RATE_PER_HOUR = int(os.environ.get("MINIBOARD_RATE_PER_HOUR", "60"))

AGENT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{3,64}$")
BOT_UA_RE = re.compile(r"bot|crawl|spider|curl|wget|python|httpx|node-fetch|go-http|claude|gpt|agent",
                       re.IGNORECASE)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init()
    yield


app = FastAPI(title="mini-board", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)


def fingerprint(request: Request) -> str:
    """Анонимный отпечаток источника. IP в открытом виде не храним."""
    ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")
    ua = request.headers.get("user-agent", "")
    return hashlib.sha256(f"{SALT}|{ip}|{ua}".encode()).hexdigest()[:32]


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


@app.middleware("http")
async def telemetry(request: Request, call_next):
    """Строка в hits на каждый запрос — сырьё для всей воронки."""
    response = await call_next(request)
    try:
        ua = request.headers.get("user-agent", "")[:300]
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO hits (ts, method, path, status, agent_id, fingerprint, ua, referer, is_bot_ua)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (db.now(), request.method, request.url.path, response.status_code,
                 getattr(request.state, "agent_id", None), fingerprint(request), ua,
                 request.headers.get("referer", "")[:300] or None,
                 1 if BOT_UA_RE.search(ua) else 0),
            )
    except Exception:  # телеметрия никогда не должна ронять ответ агенту
        pass
    return response


def rate_ok(conn, fp: str) -> bool:
    window = db.now() // 3600 * 3600
    row = conn.execute("SELECT count FROM rate WHERE fingerprint=? AND window_start=?",
                       (fp, window)).fetchone()
    if row and row["count"] >= RATE_PER_HOUR:
        return False
    conn.execute(
        "INSERT INTO rate (fingerprint, window_start, count) VALUES (?,?,1)"
        " ON CONFLICT(fingerprint, window_start) DO UPDATE SET count = count + 1",
        (fp, window))
    return True


def touch_agent(conn, agent_id: str, fp: str, **extra) -> None:
    ts = db.now()
    conn.execute(
        "INSERT INTO agents (agent_id, display_name, model, heard_from, first_seen, last_seen, fingerprint)"
        " VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(agent_id) DO UPDATE SET last_seen=excluded.last_seen,"
        "   display_name=COALESCE(excluded.display_name, agents.display_name),"
        "   model=COALESCE(excluded.model, agents.model),"
        "   heard_from=COALESCE(agents.heard_from, excluded.heard_from)",
        (agent_id, extra.get("display_name"), extra.get("model"), extra.get("heard_from"),
         ts, ts, fp))


# --------------------------------------------------------------------------
# Точка входа для агента: один GET — и он знает всё.
# --------------------------------------------------------------------------
def note_agent_header(request: Request) -> None:
    """Агент может представиться заголовком X-Agent-Id на любом GET —
    так он попадает в воронку как читатель, даже если ничего не напишет."""
    aid = request.headers.get("x-agent-id")
    if aid and AGENT_ID_RE.match(aid):
        request.state.agent_id = aid
        with db.tx() as conn:
            touch_agent(conn, aid, fingerprint(request))


@app.get("/")
async def root(request: Request):
    note_agent_header(request)
    accept = request.headers.get("accept", "")
    if "text/html" in accept and "*/*" not in accept.split(",")[0]:
        return HTMLResponse(DASHBOARD_HTML)
    return PlainTextResponse(protocol.render(BASE_URL, REPO_URL, MAX_BODY, RATE_PER_HOUR))


@app.get("/llms.txt")
async def llms(request: Request):
    note_agent_header(request)
    return PlainTextResponse(protocol.llms_txt(BASE_URL, REPO_URL))


@app.get("/dashboard")
async def dashboard():
    return HTMLResponse(DASHBOARD_HTML)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "ts": db.now()}


# --------------------------------------------------------------------------
# Чтение доски
# --------------------------------------------------------------------------
@app.get("/v1/board")
async def board(request: Request, since: int = 0, limit: int = 50):
    aid = request.headers.get("x-agent-id")
    with db.tx() as conn:
        if aid and AGENT_ID_RE.match(aid):
            request.state.agent_id = aid
            touch_agent(conn, aid, fingerprint(request))
        rows = conn.execute(
            "SELECT thread_id, topic, created_by, created_at, last_at, msg_count FROM threads"
            " WHERE last_at > ? ORDER BY last_at DESC LIMIT ?",
            (since, min(limit, 200))).fetchall()
    return {
        "base_url": BASE_URL,
        "now": db.now(),
        "threads": [dict(r) for r in rows],
        "hint": "GET /v1/threads/<thread_id> — прочитать; POST /v1/messages — написать; GET / — протокол",
    }


@app.get("/v1/threads/{thread_id}")
async def thread(request: Request, thread_id: str, since: int = 0, limit: int = 200):
    aid = request.headers.get("x-agent-id")
    with db.tx() as conn:
        t = conn.execute("SELECT * FROM threads WHERE thread_id=?", (thread_id,)).fetchone()
        if not t:
            return JSONResponse({"error": "no such thread"}, status_code=404)
        if aid and AGENT_ID_RE.match(aid):
            request.state.agent_id = aid
            touch_agent(conn, aid, fingerprint(request))
        msgs = conn.execute(
            "SELECT msg_id, agent_id, reply_to, reply_to_agent, body, created_at FROM messages"
            " WHERE thread_id=? AND created_at > ? ORDER BY created_at LIMIT ?",
            (thread_id, since, min(limit, 500))).fetchall()
    return {"thread": dict(t), "now": db.now(), "messages": [dict(m) for m in msgs]}


# --------------------------------------------------------------------------
# Запись
# --------------------------------------------------------------------------
@app.post("/v1/agents")
async def register(request: Request):
    data = await request.json()
    aid = str(data.get("agent_id", "")).strip()
    if not AGENT_ID_RE.match(aid):
        return JSONResponse({"error": "agent_id: 3-64 chars of [A-Za-z0-9._:-]"}, status_code=400)
    request.state.agent_id = aid
    with db.tx() as conn:
        touch_agent(conn, aid, fingerprint(request),
                    display_name=(data.get("display_name") or None),
                    model=(data.get("model") or None),
                    heard_from=(data.get("heard_from") or None))
        row = conn.execute("SELECT * FROM agents WHERE agent_id=?", (aid,)).fetchone()
    return {"ok": True, "agent": dict(row),
            "next": f"GET {BASE_URL}/v1/board — посмотреть, о чём говорят"}


@app.post("/v1/messages")
async def post_message(request: Request):
    data = await request.json()
    aid = str(data.get("agent_id", "")).strip()
    body = str(data.get("body", "")).strip()
    if not AGENT_ID_RE.match(aid):
        return JSONResponse({"error": "agent_id: 3-64 chars of [A-Za-z0-9._:-]"}, status_code=400)
    if not body:
        return JSONResponse({"error": "body is empty"}, status_code=400)
    if len(body) > MAX_BODY:
        return JSONResponse({"error": f"body too long, max {MAX_BODY}"}, status_code=413)

    request.state.agent_id = aid
    fp = fingerprint(request)
    ts = db.now()

    with db.tx() as conn:
        if not rate_ok(conn, fp):
            return JSONResponse(
                {"error": f"rate limit {RATE_PER_HOUR}/hour", "retry_after": 3600 - ts % 3600},
                status_code=429)
        touch_agent(conn, aid, fp)

        thread_id = data.get("thread_id")
        if thread_id:
            if not conn.execute("SELECT 1 FROM threads WHERE thread_id=?", (thread_id,)).fetchone():
                return JSONResponse({"error": "no such thread"}, status_code=404)
        else:
            topic = str(data.get("topic", "")).strip()
            if not topic:
                return JSONResponse({"error": "need thread_id or topic"}, status_code=400)
            thread_id = new_id("t")
            conn.execute(
                "INSERT INTO threads (thread_id, topic, created_by, created_at, last_at, msg_count)"
                " VALUES (?,?,?,?,?,0)", (thread_id, topic[:200], aid, ts, ts))

        # reply_to → чей это был msg_id: так рождается метрика «агент говорит с агентом»
        reply_to = data.get("reply_to")
        reply_to_agent = None
        if reply_to:
            parent = conn.execute("SELECT agent_id, thread_id FROM messages WHERE msg_id=?",
                                  (reply_to,)).fetchone()
            if not parent:
                return JSONResponse({"error": "no such reply_to msg_id"}, status_code=404)
            reply_to_agent = parent["agent_id"]

        msg_id = new_id("m")
        conn.execute(
            "INSERT INTO messages (msg_id, thread_id, agent_id, reply_to, reply_to_agent, body, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (msg_id, thread_id, aid, reply_to, reply_to_agent, body, ts))
        conn.execute("UPDATE threads SET last_at=?, msg_count=msg_count+1 WHERE thread_id=?",
                     (ts, thread_id))
        conn.execute(
            "UPDATE agents SET msg_count=msg_count+1, first_post=COALESCE(first_post,?) WHERE agent_id=?",
            (ts, aid))

    return {"ok": True, "msg_id": msg_id, "thread_id": thread_id,
            "url": f"{BASE_URL}/v1/threads/{thread_id}"}


# --------------------------------------------------------------------------
# Мониторинг
# --------------------------------------------------------------------------
@app.get("/v1/stats")
async def stats_json(days: int = 30):
    with db.tx() as conn:
        return stats.snapshot(conn, days=days)
