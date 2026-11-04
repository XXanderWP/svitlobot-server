"""Тести перевірки підписаних стуків від ESP32."""

import json

from fastapi.testclient import TestClient

from app import heartbeat

KEY = "секретний-ключ-1234567890"
BODY = json.dumps({"device": "svitlobot-esp32", "uptime_s": 5}).encode()


def test_valid_signature():
    ts = "1790000000"
    v = heartbeat.verify(KEY, ts, heartbeat.sign(KEY, ts, BODY), BODY, 1790000010, None)
    assert v.ok and v.ts == 1790000000 and v.info["uptime_s"] == 5


def test_wrong_key_rejected():
    ts = "1790000000"
    v = heartbeat.verify(KEY, ts, heartbeat.sign("інший", ts, BODY), BODY, 1790000000, None)
    assert not v.ok and "підпис" in v.reason


def test_tampered_body_rejected():
    ts = "1790000000"
    sig = heartbeat.sign(KEY, ts, BODY)
    v = heartbeat.verify(KEY, ts, sig, BODY.replace(b"5", b"6"), 1790000000, None)
    assert not v.ok


def test_stale_and_replay_rejected():
    ts = "1790000000"
    sig = heartbeat.sign(KEY, ts, BODY)
    assert not heartbeat.verify(KEY, ts, sig, BODY, 1790000000 + 301, None).ok
    v = heartbeat.verify(KEY, ts, sig, BODY, 1790000000, 1790000000)
    assert not v.ok and "повтор" in v.reason


def test_empty_server_key_rejects_everything():
    ts = "1790000000"
    assert not heartbeat.verify("", ts, heartbeat.sign("", ts, BODY), BODY, 1790000000, None).ok


def test_endpoint():
    import time

    accepted, rejected = [], []
    state = {"last": None}

    def on_accept(v, ip):
        accepted.append(ip)
        state["last"] = v.ts

    app = heartbeat.create_app(lambda: KEY, lambda: state["last"], on_accept,
                               lambda reason, ip: rejected.append(reason))
    client = TestClient(app)
    ts = str(int(time.time()))
    headers = {"X-Svitlo-Ts": ts, "X-Svitlo-Sig": heartbeat.sign(KEY, ts, BODY)}
    assert client.post("/hb", content=BODY, headers=headers).status_code == 200
    assert client.post("/hb", content=BODY, headers=headers).status_code == 401  # повтор
    assert client.post("/hb", content=BODY).status_code == 401
    assert len(accepted) == 1 and len(rejected) == 2
