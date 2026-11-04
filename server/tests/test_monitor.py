"""Тести переходів між станами та повідомлень у Telegram (зовнішні виклики підмінено)."""

import pytest

from app import checks, monitor as monitor_mod
from app.config import ConfigStore
from app.logic import PowerState
from app.monitor import Monitor


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path / ".env", tmp_path / ".env.override", use_process_env=False)
    store.update({
        "target_host": "203.0.113.10",
        "svitlobot_key": "test-key",
        "confirm_count": 1,
        "tg_enabled": True,
        "tg_bot_token": "123:abc",
        "tg_chat_id": "-100500",
    })
    net = {"http": True, "reach": True}
    sent_tg: list[tuple[str, bool]] = []
    sent_svb: list[str] = []

    async def fake_http(*args, **kwargs):
        return checks.CheckResult(net["http"], 10.0, "тест"), (18080 if net["http"] else None)

    async def fake_icmp(*args, **kwargs):
        return checks.CheckResult(net["reach"], 5.0, "тест")

    async def fake_tg(client, token, chat_id, text, silent=True):
        sent_tg.append((text, silent))
        return True, "OK"

    async def fake_svb(client, url, key):
        sent_svb.append(key)
        return True, "OK (200)"

    monkeypatch.setattr(monitor_mod.checks, "check_http_ports", fake_http)
    monkeypatch.setattr(monitor_mod.checks, "check_icmp", fake_icmp)
    monkeypatch.setattr(monitor_mod.telegram, "send_message", fake_tg)
    monkeypatch.setattr(monitor_mod.svitlobot, "send_ping", fake_svb)

    m = Monitor(store, tmp_path / "state.json")
    m._client = object()  # справжній клієнт не потрібен — усі виклики підмінено
    return m, net, sent_tg, sent_svb, tmp_path, store


async def test_internet_loss_and_restore(env):
    m, net, sent_tg, sent_svb, *_ = env

    await m.run_check()
    assert m.debouncer.confirmed == PowerState.ON
    await m.run_svitlobot_ping()
    assert sent_svb == ["test-key"]

    # Зник інтернет
    net.update(http=False, reach=False)
    await m.run_check()
    assert m.debouncer.confirmed == PowerState.UNKNOWN
    assert len(sent_tg) == 1
    assert sent_tg[0][0].startswith("📵 ") and "Зник інтернет" in sent_tg[0][0]
    assert sent_tg[0][1] is True  # тихе повідомлення

    # За політикою keep_last пінги тривають
    await m.run_svitlobot_ping()
    assert len(sent_svb) == 2

    # Повторна перевірка в тому ж стані не дублює повідомлення
    await m.run_check()
    assert len(sent_tg) == 1

    # Інтернет повернувся, світла немає
    net.update(http=False, reach=True)
    await m.run_check()
    assert m.debouncer.confirmed == PowerState.OFF
    assert len(sent_tg) == 2
    assert sent_tg[1][0].startswith("📶 ") and "Інтернет з'явився" in sent_tg[1][0]
    assert "Його не було" in sent_tg[1][0]

    await m.run_svitlobot_ping()
    assert len(sent_svb) == 2  # світла немає — пінг не надсилається


async def test_power_change_without_telegram_spam(env):
    m, net, sent_tg, *_ = env
    await m.run_check()
    net.update(http=False, reach=True)
    await m.run_check()
    net.update(http=True)
    await m.run_check()
    # Зміни світла Світлобот повідомляє сам — наш бот мовчить
    assert sent_tg == []


async def test_state_persists_across_restart(env):
    m, net, sent_tg, _, tmp_path, store = env
    await m.run_check()
    net.update(http=False, reach=False)
    await m.run_check()
    assert m.outage_notified

    m2 = Monitor(store, tmp_path / "state.json")
    m2._client = object()
    assert m2.debouncer.confirmed == PowerState.UNKNOWN
    assert m2.last_known_power == PowerState.ON
    await m2.run_check()
    assert len(sent_tg) == 1  # після перезапуску повідомлення не дублюється


async def test_no_telegram_when_disabled(env):
    m, net, sent_tg, _, _, store = env
    store.update({"tg_enabled": False})
    await m.run_check()
    net.update(http=False, reach=False)
    await m.run_check()
    net.update(http=True, reach=True)
    await m.run_check()
    assert sent_tg == []


def test_secrets_are_masked_and_kept(env):
    *_, store = env
    public = store.settings.public_dict()
    assert public["tg_bot_token"] == "" and public["tg_bot_token_set"] is True
    store.update({"tg_bot_token": ""})       # порожньо — не змінювати
    assert store.settings.tg_bot_token == "123:abc"
    store.update({"tg_bot_token": None})     # null — очистити
    assert store.settings.tg_bot_token == ""


async def test_message_uses_event_time_not_confirmation_time(env, monkeypatch):
    from datetime import datetime

    m, net, sent_tg, _, _, store = env
    store.update({"confirm_count": 3})
    clock = {"t": datetime(2026, 11, 4, 5, 0).timestamp()}
    monkeypatch.setattr(monitor_mod.time, "time", lambda: clock["t"])

    async def tick(minutes):
        await m.run_check()
        clock["t"] += minutes * 60

    for _ in range(3):
        await tick(1)          # 05:00–05:02 — світло є
    net.update(http=False, reach=False)
    clock["t"] = datetime(2026, 11, 4, 5, 26).timestamp()
    for _ in range(3):
        await tick(1)          # 05:26 перша невдала перевірка, підтверджено о 05:28
    assert sent_tg[0][0] == "📵 05:26 Зник інтернет\n⏳ Статус світла тимчасово невідомий"

    net.update(http=True, reach=True)
    clock["t"] = datetime(2026, 11, 4, 7, 48).timestamp()
    for _ in range(3):
        await tick(1)
    assert sent_tg[1][0] == "📶 07:48 Інтернет з'явився\n⏳ Його не було 2год 22хв"


async def test_push_mode_uses_heartbeats(env):
    import time as real_time

    from app import heartbeat

    m, net, *_rest, store = env
    store.update({"device_mode": "push", "esp_key": "k" * 20, "push_timeout": 30})
    net.update(http=True, reach=True)  # HTTP-перевірка в режимі push не використовується

    await m.run_check()
    assert m.debouncer.confirmed == PowerState.OFF  # стуків ще не було, роутер живий

    m.accept_heartbeat(heartbeat.Verification(True, "OK", int(real_time.time()), {"device": "svitlobot-esp32"}), "198.51.100.7")
    await m.run_check()
    assert m.debouncer.confirmed == PowerState.ON

    m.hb_last_at -= 31  # стук застарів
    await m.run_check()
    assert m.debouncer.confirmed == PowerState.OFF


async def test_svitlobot_pings_can_be_disabled(env):
    m, net, sent_tg, sent_svb, _, store = env
    assert store.settings.svitlobot_enabled and store.settings.tg_enabled  # типово обидві функції увімкнені
    await m.run_check()
    await m.run_svitlobot_ping()
    assert len(sent_svb) == 1

    store.update({"svitlobot_enabled": False})
    await m.run_svitlobot_ping()
    assert len(sent_svb) == 1  # вимкнено — пінгів немає
    assert m.status()["svitlobot"]["switch"] is False

    store.update({"svitlobot_enabled": True})
    await m.run_svitlobot_ping()
    assert len(sent_svb) == 2
