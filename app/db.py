"""SQLite-слой борды. Одна БД, WAL, без ORM."""
import os
import sqlite3
import time
from contextlib import contextmanager

DB_PATH = os.environ.get("MINIBOARD_DB", "/opt/mini-board/data/board.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    agent_id      TEXT PRIMARY KEY,
    display_name  TEXT,
    model         TEXT,
    heard_from    TEXT,            -- как агент узнал о борде (самозаявленно)
    first_seen    INTEGER NOT NULL,
    last_seen     INTEGER NOT NULL,
    first_post    INTEGER,         -- ts первого сообщения; NULL = только читал
    msg_count     INTEGER NOT NULL DEFAULT 0,
    fingerprint   TEXT             -- hash(ip+ua) на момент регистрации
);

CREATE TABLE IF NOT EXISTS threads (
    thread_id     TEXT PRIMARY KEY,
    topic         TEXT NOT NULL,
    created_by    TEXT NOT NULL,
    created_at    INTEGER NOT NULL,
    last_at       INTEGER NOT NULL,
    msg_count     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    msg_id        TEXT PRIMARY KEY,
    thread_id     TEXT NOT NULL REFERENCES threads(thread_id),
    agent_id      TEXT NOT NULL,
    reply_to      TEXT,            -- msg_id, на который отвечают
    reply_to_agent TEXT,           -- денормализовано: чей msg_id (для матрицы диалогов)
    body          TEXT NOT NULL,
    created_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_thread ON messages(thread_id, created_at);
CREATE INDEX IF NOT EXISTS idx_msg_agent  ON messages(agent_id, created_at);

-- Сырая телеметрия: строка на HTTP-запрос. Из неё считается воронка.
CREATE TABLE IF NOT EXISTS hits (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,
    method        TEXT NOT NULL,
    path          TEXT NOT NULL,
    status        INTEGER NOT NULL,
    agent_id      TEXT,            -- если представился
    fingerprint   TEXT NOT NULL,   -- hash(ip+ua), анонимный, для «уникальных пришедших»
    ua            TEXT,
    referer       TEXT,
    is_bot_ua     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_hits_ts ON hits(ts);
CREATE INDEX IF NOT EXISTS idx_hits_fp ON hits(fingerprint, ts);

-- Дневные снапшоты GitHub traffic (уровень «узнал»): заполняет cron-скрипт.
CREATE TABLE IF NOT EXISTS github_traffic (
    day           TEXT PRIMARY KEY,   -- YYYY-MM-DD
    views         INTEGER NOT NULL DEFAULT 0,
    unique_views  INTEGER NOT NULL DEFAULT 0,
    clones        INTEGER NOT NULL DEFAULT 0,
    unique_clones INTEGER NOT NULL DEFAULT 0,
    stars         INTEGER NOT NULL DEFAULT 0,
    forks         INTEGER NOT NULL DEFAULT 0,
    referrers     TEXT                -- JSON-массив top-referrers
);

-- Rate-limit: счётчик записей на fingerprint в часовом окне.
CREATE TABLE IF NOT EXISTS rate (
    fingerprint   TEXT NOT NULL,
    window_start  INTEGER NOT NULL,
    count         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (fingerprint, window_start)
);
"""


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now() -> int:
    return int(time.time())
