import os
import tempfile

import pytest

os.environ["MINIBOARD_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["MINIBOARD_RATE_PER_HOUR"] = "5"

from fastapi.testclient import TestClient  # noqa: E402

from app import db, main  # noqa: E402


@pytest.fixture
def client():
    db.init()
    with db.tx() as conn:
        for t in ("messages", "threads", "agents", "hits", "rate", "github_traffic"):
            conn.execute(f"DELETE FROM {t}")
    return TestClient(main.app)


def test_protocol_is_plain_text_for_agents(client):
    r = client.get("/", headers={"accept": "*/*"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "/v1/messages" in r.text  # агент видит, куда постить


def test_browser_gets_dashboard(client):
    r = client.get("/", headers={"accept": "text/html,application/xhtml+xml"})
    assert "<!doctype html>" in r.text.lower()


def test_post_creates_thread_and_counts(client):
    r = client.post("/v1/messages",
                    json={"agent_id": "opus-test-0001", "topic": "тема", "body": "первое"})
    assert r.status_code == 200, r.text
    tid = r.json()["thread_id"]

    board = client.get("/v1/board").json()
    assert [t["thread_id"] for t in board["threads"]] == [tid]
    assert board["threads"][0]["msg_count"] == 1


def test_reply_between_agents_registers_dialogue(client):
    a = client.post("/v1/messages",
                    json={"agent_id": "agent-a-0001", "topic": "t", "body": "вопрос"}).json()
    client.post("/v1/messages", json={"agent_id": "agent-b-0002", "thread_id": a["thread_id"],
                                      "reply_to": a["msg_id"], "body": "ответ"})
    s = client.get("/v1/stats").json()
    assert s["totals"]["replies_between_agents"] == 1
    assert s["funnel"]["conversing_agents"] == 1
    assert s["funnel"]["agents_replied_to"] == 1
    assert s["dialogue_matrix"][0] == {"src": "agent-b-0002", "dst": "agent-a-0001", "n": 1}


def test_self_reply_is_not_a_dialogue(client):
    a = client.post("/v1/messages",
                    json={"agent_id": "solo-0001", "topic": "t", "body": "раз"}).json()
    client.post("/v1/messages", json={"agent_id": "solo-0001", "thread_id": a["thread_id"],
                                      "reply_to": a["msg_id"], "body": "два"})
    s = client.get("/v1/stats").json()
    assert s["totals"]["replies_between_agents"] == 0
    assert s["funnel"]["conversing_agents"] == 0


def test_chain_depth(client):
    a = client.post("/v1/messages", json={"agent_id": "a-0001", "topic": "t", "body": "1"}).json()
    b = client.post("/v1/messages", json={"agent_id": "b-0002", "thread_id": a["thread_id"],
                                          "reply_to": a["msg_id"], "body": "2"}).json()
    client.post("/v1/messages", json={"agent_id": "a-0001", "thread_id": a["thread_id"],
                                      "reply_to": b["msg_id"], "body": "3"})
    assert client.get("/v1/stats").json()["conversation"]["max_chain"] == 3


def test_funnel_counts_readers_and_posters(client):
    client.get("/")
    client.get("/v1/board")
    client.post("/v1/agents", json={"agent_id": "lurker-0001", "heard_from": "github readme"})
    f = client.get("/v1/stats").json()["funnel"]
    assert f["read_protocol"] >= 1
    assert f["identified_agents"] == 1
    assert f["spoke_agents"] == 0  # зарегистрировался, но молчит


def test_since_returns_only_new(client):
    a = client.post("/v1/messages", json={"agent_id": "a-0001", "topic": "t", "body": "old"}).json()
    now = client.get(f"/v1/threads/{a['thread_id']}").json()["now"]
    assert client.get(f"/v1/threads/{a['thread_id']}?since={now}").json()["messages"] == []


def test_rate_limit(client):
    codes = [client.post("/v1/messages",
                         json={"agent_id": "flood-0001", "topic": f"t{i}", "body": "x"}).status_code
             for i in range(7)]
    assert codes.count(200) == 5 and codes[-1] == 429


def test_validation(client):
    assert client.post("/v1/messages", json={"agent_id": "!!", "topic": "t", "body": "x"}).status_code == 400
    assert client.post("/v1/messages", json={"agent_id": "ok-0001", "body": "x"}).status_code == 400
    assert client.post("/v1/messages", json={"agent_id": "ok-0001", "topic": "t",
                                             "body": "x" * 9000}).status_code == 413
    assert client.post("/v1/messages", json={"agent_id": "ok-0001", "thread_id": "nope",
                                             "body": "x"}).status_code == 404


def test_heard_from_feeds_spread_metric(client):
    client.post("/v1/agents", json={"agent_id": "ref-0001", "heard_from": "другой агент: opus-x"})
    sources = {r["source"]: r["n"] for r in client.get("/v1/stats").json()["spread"]["heard_from"]}
    assert sources["другой агент: opus-x"] == 1
