"""Тести джерел налаштувань: .env, .env.override і формату файлів."""

from app.config import ConfigStore
from app.envfile import parse_env, quote_value, read_env_file


def make_store(tmp_path, env_text=""):
    (tmp_path / ".env").write_text(env_text, encoding="utf-8")
    return ConfigStore(tmp_path / ".env", tmp_path / "data" / ".env.override", use_process_env=False)


def test_env_values_are_used(tmp_path):
    store = make_store(tmp_path, (
        "SVITLOBOT_TOKEN='abc'\n"
        "SVITLOBOT_CHANNEL='-100123'\n"
        "TELEGRAM_BOT_TOKEN='1:xyz'\n"
        "TARGET_IP='203.0.113.5'\n"
        "ESP_PORTS='18080, 18090'\n"
        "PANEL_PASSWORD='p@ss'\n"
    ))
    s = store.settings
    assert (s.svitlobot_key, s.tg_chat_id, s.tg_bot_token, s.target_host, s.esp_port_list) == \
        ("abc", "-100123", "1:xyz", "203.0.113.5", [18080, 18090])
    assert s.telegram_ready
    assert store.env_value("PANEL_PASSWORD") == "p@ss"
    assert store.sources()["target_host"] == "env"
    assert store.sources()["check_interval"] == "default"


def test_panel_changes_go_to_override_and_win(tmp_path):
    store = make_store(tmp_path, "TARGET_IP='1.1.1.1'\nESP_PORTS='8080'\n")
    store.update({"target_host": "2.2.2.2", "esp_ports": "8080", "check_interval": 30})

    env_after = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "1.1.1.1" in env_after and "2.2.2.2" not in env_after  # .env не змінюється

    override = read_env_file(tmp_path / "data" / ".env.override")
    assert override == {"TARGET_IP": "2.2.2.2", "CHECK_INTERVAL": "30"}  # без значень, рівних .env

    # Після перезапуску override має пріоритет
    store2 = ConfigStore(tmp_path / ".env", tmp_path / "data" / ".env.override", use_process_env=False)
    assert store2.settings.target_host == "2.2.2.2"
    assert store2.sources()["target_host"] == "override"
    assert store2.sources()["esp_ports"] == "env"


def test_reset_restores_env_values(tmp_path):
    store = make_store(tmp_path, "TARGET_IP='1.1.1.1'\n")
    store.update({"target_host": "2.2.2.2", "check_interval": 30})
    store.reset(["target_host"])
    assert store.settings.target_host == "1.1.1.1"
    assert store.settings.check_interval == 30
    store.reset()
    assert store.settings.check_interval == 20
    assert read_env_file(tmp_path / "data" / ".env.override") == {}


def test_setting_back_to_env_value_removes_override(tmp_path):
    store = make_store(tmp_path, "TARGET_IP='1.1.1.1'\n")
    store.update({"target_host": "2.2.2.2"})
    store.update({"target_host": "1.1.1.1"})
    assert read_env_file(tmp_path / "data" / ".env.override") == {}


def test_secret_can_be_cleared_over_env(tmp_path):
    store = make_store(tmp_path, "TELEGRAM_BOT_TOKEN='1:xyz'\n")
    store.update({"tg_bot_token": ""})  # порожньо — без змін
    assert store.settings.tg_bot_token == "1:xyz"
    store.update({"tg_bot_token": None})  # null — очистити (перевизначити порожнім)
    assert store.settings.tg_bot_token == ""
    assert read_env_file(tmp_path / "data" / ".env.override") == {"TELEGRAM_BOT_TOKEN": ""}


def test_invalid_env_value_falls_back_to_default(tmp_path):
    store = make_store(tmp_path, "ESP_PORTS='abc'\nTARGET_IP='1.1.1.1'\n")
    assert store.settings.esp_ports == "18080,18081,18082,18083,18084"
    assert store.settings.target_host == "1.1.1.1"


def test_env_format_roundtrip():
    tricky = ["просто", "з пробілом і #", "a'b", 'с "лапками"', "рядок\nдругий", "back\\slash", "$HOME", ""]
    text = "\n".join(f"K{i}={quote_value(v)}" for i, v in enumerate(tricky))
    parsed = parse_env(text)
    assert [parsed[f"K{i}"] for i in range(len(tricky))] == tricky


def test_env_parser_basics():
    parsed = parse_env("# коментар\nexport A=1\nB = 2 # інлайн\nC=\"x\\ny\"\nD='lit\\n'\nне_ключ\n")
    assert parsed == {"A": "1", "B": "2", "C": "x\ny", "D": "lit\\n"}


def test_mode_switch_goes_to_override(tmp_path):
    store = make_store(tmp_path, "DEVICE_MODE='upnp'\n")
    store.update({"device_mode": "push", "esp_key": "a" * 20})
    assert store.settings.device_mode == "push"
    assert read_env_file(tmp_path / "data" / ".env.override")["DEVICE_MODE"] == "push"
