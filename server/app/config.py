"""Налаштування сервісу.

Джерела (кожне наступне має вищий пріоритет):
1. типові значення з моделі `Settings`;
2. змінні оточення процесу;
3. файл `.env` (на сервері його формує GitHub Action із секретів);
4. файл `.env.override` — зміни, зроблені у веб-панелі.

`.env` сервіс лише читає, а пише тільки в `.env.override`.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

from .envfile import read_env_file, write_env_file
from .logic import UnknownPolicy

log = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
ENV_FILE = Path(os.environ.get("ENV_FILE", "/config/.env"))
OVERRIDE_FILE = Path(os.environ.get("OVERRIDE_FILE", str(DATA_DIR / ".env.override")))

DEFAULT_SVITLOBOT_URL = "https://api.svitlobot.in.ua/channelPing?channel_key={key}"

# Формат повторює повідомлення Світлобота («🟢 02:28 Світло з'явилося / 🕓 Його не було …»),
# але з іншими емодзі, щоб повідомлення про інтернет не плутали зі світлом.
DEFAULT_TEXT_LOST = "📵 {time} Зник інтернет\n⏳ Статус світла тимчасово невідомий"
DEFAULT_TEXT_RESTORED = "📶 {time} Інтернет з'явився\n⏳ Його не було {duration}"

# Зовнішні порти-кандидати (мають збігатися з --upnp-ports при прошивці).
DEFAULT_ESP_PORTS = "18080,18081,18082,18083,18084"

# Поля, значення яких не віддаються у веб-інтерфейс.
SECRET_FIELDS = ("svitlobot_key", "esp_key", "tg_bot_token")


class Settings(BaseModel):
    # Світлобот
    svitlobot_enabled: bool = True
    svitlobot_key: str = ""
    svitlobot_url: str = DEFAULT_SVITLOBOT_URL
    ping_interval: int = Field(60, ge=10, le=600)

    # Режим роботи ESP32: upnp — сервер сам звертається до сторінки ESP32 (порт відкрито через
    # UPnP або вручну); push — ESP32 сам стукає на сервер.
    device_mode: str = Field("upnp", pattern="^(upnp|push)$")

    # Об'єкт перевірки
    target_host: str = ""
    esp_ports: str = DEFAULT_ESP_PORTS
    esp_path: str = "/status"
    esp_key: str = ""
    http_timeout: float = Field(5.0, ge=1, le=30)
    reachability_method: str = Field("icmp", pattern="^(icmp|tcp)$")
    tcp_port: int = Field(443, ge=1, le=65535)
    ping_timeout: float = Field(3.0, ge=1, le=30)
    check_interval: int = Field(20, ge=5, le=600)
    push_timeout: int = Field(60, ge=10, le=900)
    confirm_count: int = Field(3, ge=1, le=20)
    unknown_policy: UnknownPolicy = UnknownPolicy.KEEP_LAST

    # Telegram
    tg_enabled: bool = True
    tg_bot_token: str = ""
    tg_chat_id: str = ""
    tg_silent: bool = True
    tg_text_lost: str = DEFAULT_TEXT_LOST
    tg_text_restored: str = DEFAULT_TEXT_RESTORED

    @field_validator("target_host", "svitlobot_key", "tg_chat_id", "tg_bot_token", "esp_key")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("esp_path")
    @classmethod
    def _path(cls, v: str) -> str:
        v = v.strip() or "/"
        return v if v.startswith("/") else "/" + v

    @field_validator("esp_ports")
    @classmethod
    def _ports(cls, v: str) -> str:
        ports: list[int] = []
        for item in v.replace(" ", "").split(","):
            if not item:
                continue
            if not item.isdigit() or not 1 <= int(item) <= 65535:
                raise ValueError(f"некоректний порт: {item}")
            if int(item) not in ports:
                ports.append(int(item))
        if not ports:
            raise ValueError("вкажіть хоча б один порт")
        if len(ports) > 10:
            raise ValueError("не більше 10 портів")
        return ",".join(str(p) for p in ports)

    @property
    def esp_port_list(self) -> list[int]:
        return [int(p) for p in self.esp_ports.split(",")]

    @field_validator("svitlobot_url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError("URL має починатися з http:// або https://")
        if "{key}" not in v:
            raise ValueError("URL має містити місце для ключа {key}")
        return v

    @property
    def telegram_ready(self) -> bool:
        return self.tg_enabled and bool(self.tg_bot_token) and bool(self.tg_chat_id)

    @property
    def monitoring_ready(self) -> bool:
        return bool(self.target_host)

    def public_dict(self) -> dict[str, Any]:
        """Налаштування для веб-інтерфейсу: секрети замінено на ознаку «задано»."""
        data = self.model_dump(mode="json")
        for name in SECRET_FIELDS:
            data[f"{name}_set"] = bool(data[name])
            data[name] = ""
        return data


FIELD_LABELS = {
    "svitlobot_url": "URL API Світлобота",
    "ping_interval": "Інтервал пінгів Світлобота",
    "target_host": "Адреса об'єкта",
    "device_mode": "Режим роботи ESP32",
    "esp_ports": "Порти ESP32",
    "push_timeout": "Тайм-аут стуку",
    "esp_path": "Шлях сторінки ESP32",
    "http_timeout": "Тайм-аут HTTP",
    "reachability_method": "Спосіб перевірки доступності",
    "tcp_port": "TCP-порт",
    "ping_timeout": "Тайм-аут пінгу",
    "check_interval": "Інтервал перевірок",
    "confirm_count": "Кількість підтверджень",
    "unknown_policy": "Поведінка при невідомому стані",
}


def _error_message(err: dict[str, Any]) -> str:
    kind = err.get("type", "")
    ctx = err.get("ctx") or {}
    if kind == "greater_than_equal":
        return f"має бути не менше {ctx.get('ge')}"
    if kind == "less_than_equal":
        return f"має бути не більше {ctx.get('le')}"
    if kind in ("int_parsing", "int_type", "float_parsing", "float_type", "int_from_float"):
        return "має бути числом"
    if kind in ("bool_parsing", "bool_type"):
        return "має бути «так» або «ні»"
    if kind in ("string_pattern_mismatch", "enum", "literal_error"):
        return "недопустиме значення"
    if kind == "string_type":
        return "має бути рядком"
    if kind == "value_error":
        return str(ctx.get("error", err.get("msg")))
    return str(err.get("msg"))


def format_validation_error(exc: ValidationError) -> str:
    messages = []
    for err in exc.errors():
        field = ".".join(str(x) for x in err.get("loc", ()))
        messages.append(f"{FIELD_LABELS.get(field, field)}: {_error_message(err)}")
    return "; ".join(messages)


# Відповідність полів налаштувань змінним у .env / .env.override.
ENV_KEYS: dict[str, str] = {
    "svitlobot_enabled": "SVITLOBOT_ENABLED",
    "svitlobot_key": "SVITLOBOT_TOKEN",
    "svitlobot_url": "SVITLOBOT_URL",
    "ping_interval": "SVITLOBOT_PING_INTERVAL",
    "target_host": "TARGET_IP",
    "device_mode": "DEVICE_MODE",
    "esp_ports": "ESP_PORTS",
    "push_timeout": "PUSH_TIMEOUT",
    "esp_path": "ESP_PATH",
    "esp_key": "ESP_KEY",
    "http_timeout": "HTTP_TIMEOUT",
    "reachability_method": "REACHABILITY_METHOD",
    "tcp_port": "TCP_PORT",
    "ping_timeout": "PING_TIMEOUT",
    "check_interval": "CHECK_INTERVAL",
    "confirm_count": "CONFIRM_COUNT",
    "unknown_policy": "UNKNOWN_POLICY",
    "tg_enabled": "TELEGRAM_ENABLED",
    "tg_bot_token": "TELEGRAM_BOT_TOKEN",
    "tg_chat_id": "SVITLOBOT_CHANNEL",
    "tg_silent": "TELEGRAM_SILENT",
    "tg_text_lost": "TELEGRAM_TEXT_LOST",
    "tg_text_restored": "TELEGRAM_TEXT_RESTORED",
}
assert set(ENV_KEYS) == set(Settings.model_fields), "ENV_KEYS має охоплювати всі поля Settings"

OVERRIDE_HEADER = (
    "Зміни, зроблені у веб-панелі SvitloBot Monitor.\n"
    "Значення тут мають пріоритет над .env. Видаліть рядок, щоб повернути значення з .env.\n"
    "Файл перезаписується панеллю — редагуйте обережно."
)


def _to_env_string(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if hasattr(value, "value"):  # Enum
        return str(value.value)
    return str(value)


def _build_settings(raw: dict[str, str], origin: str) -> tuple[Settings, list[str]]:
    """Створює Settings, відкидаючи некоректні значення (з попередженням у лог)."""
    data = {field: raw[env] for field, env in ENV_KEYS.items() if env in raw}
    dropped: list[str] = []
    while True:
        try:
            return Settings.model_validate(data), dropped
        except ValidationError as exc:
            bad = {str(err["loc"][0]) for err in exc.errors() if err.get("loc")}
            if not bad or not bad & data.keys():
                raise
            for field in bad & data.keys():
                log.error("Некоректне значення %s у %s: %s — використовується типове",
                          ENV_KEYS[field], origin, format_validation_error(exc))
                data.pop(field)
                dropped.append(field)


class ConfigStore:
    """Потокобезпечне сховище налаштувань: читає .env, пише зміни панелі в .env.override."""

    def __init__(self, env_file: Path | None = None, override_file: Path | None = None,
                 use_process_env: bool = True) -> None:
        self.env_file = env_file or ENV_FILE
        self.override_file = override_file or OVERRIDE_FILE
        self.use_process_env = use_process_env
        self._lock = threading.Lock()
        self.reload()

    def reload(self) -> None:
        with self._lock:
            base_raw: dict[str, str] = {}
            if self.use_process_env:
                base_raw.update({k: v for k, v in os.environ.items() if k in ENV_KEYS.values()})
            self._env_raw = read_env_file(self.env_file)
            base_raw.update(self._env_raw)
            self._base_raw = base_raw
            self._base, _ = _build_settings(base_raw, str(self.env_file))
            override = read_env_file(self.override_file)
            # Лишаємо в override тільки відомі ключі.
            self._override = {k: v for k, v in override.items() if k in ENV_KEYS.values()}
            self._settings, dropped = _build_settings({**base_raw, **self._override}, str(self.override_file))
            for field in dropped:
                self._override.pop(ENV_KEYS[field], None)

    @property
    def settings(self) -> Settings:
        with self._lock:
            return self._settings

    def env_value(self, key: str, default: str = "") -> str:
        """Значення довільної змінної з .env або оточення (напр. PANEL_PASSWORD)."""
        file_value = read_env_file(self.env_file).get(key)
        if file_value is not None:
            return file_value
        return os.environ.get(key, default) if self.use_process_env else default

    def sources(self) -> dict[str, str]:
        """Звідки взято кожне поле: default, env або override."""
        with self._lock:
            result = {}
            for field, env in ENV_KEYS.items():
                if env in self._override:
                    result[field] = "override"
                elif env in self._base_raw:
                    result[field] = "env"
                else:
                    result[field] = "default"
            return result

    def public_dict(self) -> dict[str, Any]:
        data = self.settings.public_dict()
        data["_sources"] = self.sources()
        data["_env_keys"] = ENV_KEYS
        return data

    def update(self, changes: dict[str, Any]) -> Settings:
        """Застосовує зміни з панелі.

        Для секретних полів: порожній рядок — залишити як є, null — очистити.
        Значення, що збігається зі значенням з .env, прибирається з override.
        """
        with self._lock:
            current = self._settings.model_dump()
            for key, value in changes.items():
                if key not in Settings.model_fields:
                    continue
                if key in SECRET_FIELDS:
                    if value == "":
                        continue
                    if value is None:
                        value = ""
                current[key] = value
            new = Settings.model_validate(current)  # може кинути ValidationError

            base = self._base.model_dump()
            new_dump = new.model_dump()
            override = dict(self._override)
            for field, env in ENV_KEYS.items():
                if new_dump[field] == base[field]:
                    override.pop(env, None)
                else:
                    override[env] = _to_env_string(new_dump[field])
            self._write_override(override)
            self._override = override
            self._settings = new
            return new

    def reset(self, fields: list[str] | None = None) -> Settings:
        """Прибирає зміни панелі для вказаних полів (або всіх) — повертаються значення з .env."""
        with self._lock:
            override = dict(self._override)
            targets = fields if fields is not None else list(ENV_KEYS)
            for field in targets:
                if field in ENV_KEYS:
                    override.pop(ENV_KEYS[field], None)
            self._write_override(override)
        self.reload()
        return self.settings

    def _write_override(self, override: dict[str, str]) -> None:
        write_env_file(self.override_file, override, OVERRIDE_HEADER)
