"""Фонові цикли моніторингу: перевірки об'єкта та пінги Світлобота."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx

from . import __version__, checks, heartbeat, svitlobot, telegram
from .config import DEFAULT_TEXT_LOST, DEFAULT_TEXT_RESTORED, ConfigStore, Settings
from .logic import (
    STATE_LABELS,
    Debouncer,
    PowerState,
    Transition,
    classify,
    format_duration,
    should_ping_svitlobot,
)

log = logging.getLogger(__name__)

MAX_EVENTS = 200
NOTIFY_RETRY_SECONDS = 60


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _fmt_ts(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().strftime("%H:%M")


def _ddmm(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().strftime("%d.%m.%Y")


def _label(state: Optional[PowerState]) -> str:
    return STATE_LABELS.get(state, "невідомо") if state else "невідомо"


def render_template(template: str, fallback: str, values: dict[str, str]) -> str:
    """Підставляє значення в шаблон; невідомі змінні лишаються як є."""
    safe = defaultdict(str, values)
    try:
        return template.format_map(safe)
    except (ValueError, IndexError):
        log.warning("Некоректний шаблон повідомлення, використовується типовий")
        return fallback.format_map(safe)


class Monitor:
    def __init__(self, store: ConfigStore, state_path: Path) -> None:
        self.store = store
        self.state_path = state_path

        self.debouncer = Debouncer()
        self.last_known_power: Optional[PowerState] = None
        self.state_changed_at: Optional[float] = None
        self.unknown_since: Optional[float] = None
        self.outage_notified = False
        self._last_notify_attempt = 0.0
        self._candidate_since: Optional[float] = None

        self.last_check_at: Optional[float] = None
        self.last_observed: Optional[PowerState] = None
        self.last_http: Optional[checks.CheckResult] = None
        self.last_reach: Optional[checks.CheckResult] = None

        self.svb_active = False
        self.svb_last_at: Optional[float] = None
        self.svb_last_ok: Optional[bool] = None
        self.svb_last_detail = ""
        self.svb_consecutive_errors = 0

        # Режим «проброс порту»: на якому зовнішньому порту востаннє відповіла ESP32.
        self.esp_good_port: Optional[int] = None

        # Режим «стук»: останній прийнятий і останній відхилений стук.
        self.hb_last_at: Optional[float] = None
        self.hb_last_ts: Optional[int] = None
        self.hb_last_info: dict[str, Any] = {}
        self.hb_last_ip = ""
        self.hb_reject_at: Optional[float] = None
        self.hb_reject_reason = ""
        self.hb_reject_ip = ""
        self._hb_reject_logged_at = 0.0

        self.events: deque[dict[str, str]] = deque(maxlen=MAX_EVENTS)

        self._wake_check = asyncio.Event()
        self._wake_ping = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._client: Optional[httpx.AsyncClient] = None
        self._check_lock = asyncio.Lock()

        self._load_state()

    # ------------------------------------------------------------------
    # Збереження стану між перезапусками
    # ------------------------------------------------------------------

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.error("Не вдалося прочитати стан %s: %s", self.state_path, exc)
            return

        def parse(value: Any) -> Optional[PowerState]:
            try:
                return PowerState(value) if value else None
            except ValueError:
                return None

        self.debouncer.confirmed = parse(data.get("confirmed"))
        self.last_known_power = parse(data.get("last_known_power"))
        self.state_changed_at = data.get("state_changed_at")
        self.unknown_since = data.get("unknown_since")
        self.outage_notified = bool(data.get("outage_notified"))
        self.hb_last_ts = data.get("hb_last_ts")
        self.esp_good_port = data.get("esp_good_port")
        for event in data.get("events", [])[-MAX_EVENTS:]:
            if isinstance(event, dict):
                self.events.append(event)

    def _save_state(self) -> None:
        data = {
            "confirmed": self.debouncer.confirmed.value if self.debouncer.confirmed else None,
            "last_known_power": self.last_known_power.value if self.last_known_power else None,
            "state_changed_at": self.state_changed_at,
            "unknown_since": self.unknown_since,
            "outage_notified": self.outage_notified,
            "hb_last_ts": self.hb_last_ts,
            "esp_good_port": self.esp_good_port,
            "events": list(self.events),
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError as exc:
            log.error("Не вдалося зберегти стан: %s", exc)

    def add_event(self, text: str, level: str = "info") -> None:
        self.events.append({"time": _now_iso(), "level": level, "text": text})
        getattr(log, "warning" if level == "warn" else level, log.info)(text)

    # ------------------------------------------------------------------
    # Життєвий цикл
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            headers={"User-Agent": f"SvitloBot-Monitor/{__version__}"},
            follow_redirects=False,
        )
        self.add_event(f"Сервіс запущено (версія {__version__})")
        self._tasks = [
            asyncio.create_task(self._check_loop(), name="check-loop"),
            asyncio.create_task(self._ping_loop(), name="ping-loop"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._client:
            await self._client.aclose()
        self._save_state()

    def wake(self) -> None:
        """Застосувати нові налаштування негайно, не чекаючи кінця інтервалу."""
        self._wake_check.set()
        self._wake_ping.set()

    @staticmethod
    async def _sleep(event: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        finally:
            event.clear()

    async def _check_loop(self) -> None:
        while True:
            try:
                await self.run_check()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Помилка в циклі перевірок")
            await self._sleep(self._wake_check, self.store.settings.check_interval)

    async def _ping_loop(self) -> None:
        while True:
            try:
                await self.run_svitlobot_ping()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Помилка в циклі пінгів Світлобота")
            await self._sleep(self._wake_ping, self.store.settings.ping_interval)

    # ------------------------------------------------------------------
    # Перевірки
    # ------------------------------------------------------------------

    async def run_check(self) -> None:
        async with self._check_lock:
            s = self.store.settings
            if not s.monitoring_ready:
                return
            assert self._client is not None

            http_res, reach_res = await self._probe(s)

            observed = classify(http_res.ok, reach_res.ok)
            self.last_check_at = time.time()
            self.last_http, self.last_reach, self.last_observed = http_res, reach_res, observed

            checked_at = self.last_check_at
            transition = self.debouncer.feed(observed, s.confirm_count)
            if transition:
                # Подія сталася під час першої перевірки з новим результатом, а не в момент підтвердження.
                event_at = self._candidate_since if s.confirm_count > 1 and self._candidate_since else checked_at
                self._candidate_since = None
                await self._on_transition(transition, s, event_at)
            elif self.debouncer.count == 1:
                self._candidate_since = checked_at
            elif self.debouncer.count == 0:
                self._candidate_since = None
            await self._ensure_outage_notice(s)
            self._save_state()

    async def _probe(self, s: Settings) -> tuple[checks.CheckResult, checks.CheckResult]:
        """Паралельно перевіряє ESP32 (сторінку або свіжість стуку) і доступність IP."""
        assert self._client is not None
        if s.reachability_method == "tcp":
            reach_coro = checks.check_tcp(s.target_host, s.tcp_port, s.ping_timeout)
        else:
            reach_coro = checks.check_icmp(s.target_host, s.ping_timeout)

        if s.device_mode == "push":
            reach_res = await reach_coro
            return self._heartbeat_result(s), reach_res

        (http_res, port), reach_res = await asyncio.gather(
            checks.check_http_ports(self._client, s.target_host, s.esp_port_list, self.esp_good_port,
                                    s.esp_path, s.esp_key, s.http_timeout),
            reach_coro,
        )
        if port and port != self.esp_good_port:
            if self.esp_good_port:
                self.add_event(f"ESP32 тепер відповідає на зовнішньому порту {port}")
            self.esp_good_port = port
        return http_res, reach_res

    def _heartbeat_result(self, s: Settings) -> checks.CheckResult:
        """Стан ESP32 у режимі стуку: чи був підписаний стук за останні push_timeout секунд."""
        now = time.time()
        if self.hb_last_at is None:
            detail = "стуків від ESP32 ще не було"
            if self.hb_reject_at and now - self.hb_reject_at < s.push_timeout:
                detail += f"; відхилено запит: {self.hb_reject_reason}"
            return checks.CheckResult(False, None, detail)
        age = now - self.hb_last_at
        ok = age <= s.push_timeout
        detail = "останній стук щойно" if age < 5 else f"останній стук {format_duration(age)} тому"
        if not ok and self.hb_reject_at and self.hb_reject_at > self.hb_last_at:
            detail += f"; після нього відхилено запит: {self.hb_reject_reason}"
        return checks.CheckResult(ok, None, detail)

    # ------------------------------------------------------------------
    # Приймання стуків (викликається сервером стуків у тому ж циклі подій)
    # ------------------------------------------------------------------

    def heartbeat_key(self) -> str:
        return self.store.settings.esp_key

    def heartbeat_last_ts(self) -> Optional[int]:
        return self.hb_last_ts

    def accept_heartbeat(self, result: heartbeat.Verification, client_ip: str) -> None:
        first = self.hb_last_at is None or time.time() - self.hb_last_at > self.store.settings.push_timeout
        self.hb_last_at = time.time()
        self.hb_last_ts = result.ts
        self.hb_last_info = result.info or {}
        self.hb_last_ip = client_ip
        if first:
            self.add_event(f"ESP32 стукає на сервер (з адреси {client_ip})")
        # Якщо світла не було — перевіряємо одразу, а не чекаємо кінця інтервалу.
        if self.store.settings.device_mode == "push" and self.debouncer.confirmed != PowerState.ON:
            self._wake_check.set()

    def reject_heartbeat(self, reason: str, client_ip: str) -> None:
        now = time.time()
        changed = reason != self.hb_reject_reason
        self.hb_reject_at = now
        self.hb_reject_reason = reason
        self.hb_reject_ip = client_ip
        # Не засмічуємо журнал: однакова причина — не частіше ніж раз на 10 хвилин.
        if changed or now - self._hb_reject_logged_at > 600:
            self._hb_reject_logged_at = now
            self.add_event(f"Відхилено стук з {client_ip}: {reason}", "warn")

    async def _on_transition(self, t: Transition, s: Settings, now: float) -> None:
        self.state_changed_at = now
        prev, cur = t.previous, t.current

        if cur == PowerState.UNKNOWN:
            self.unknown_since = now
            self.outage_notified = False
            self._last_notify_attempt = 0.0
            self.add_event("Зв'язок з об'єктом втрачено: ні сторінка ESP32, ні IP не відповідають", "warn")
            return

        if cur == PowerState.ON:
            self.add_event("Світло є: ESP32 на зв'язку")
        else:
            self.add_event("Світла немає: IP відповідає, а ESP32 — ні", "warn")

        if prev == PowerState.UNKNOWN:
            duration = format_duration(now - self.unknown_since) if self.unknown_since else "невідомо скільки"
            self.add_event(f"Зв'язок з об'єктом відновлено (не було {duration})")
            if self.outage_notified and s.telegram_ready:
                text = render_template(s.tg_text_restored, DEFAULT_TEXT_RESTORED, {
                    "status": _label(cur),
                    "last_status": _label(self.last_known_power),
                    "duration": duration,
                    "time": _hhmm(now),
                    "date": _ddmm(now),
                })
                await self._notify(s, text)

        self.unknown_since = None
        self.outage_notified = False
        self.last_known_power = cur
        # Світлобот має дізнатися про зміну якнайшвидше.
        self._wake_ping.set()

    async def _ensure_outage_notice(self, s: Settings) -> None:
        """Надсилає повідомлення про втрату зв'язку (з повторами, якщо Telegram недоступний)."""
        if self.debouncer.confirmed != PowerState.UNKNOWN or self.outage_notified or not s.telegram_ready:
            return
        if time.time() - self._last_notify_attempt < NOTIFY_RETRY_SECONDS:
            return
        self._last_notify_attempt = time.time()
        text = render_template(s.tg_text_lost, DEFAULT_TEXT_LOST, {
            "status": _label(PowerState.UNKNOWN),
            "last_status": _label(self.last_known_power),
            "duration": format_duration(time.time() - (self.unknown_since or time.time())),
            "time": _hhmm(self.unknown_since or time.time()),
            "date": _ddmm(self.unknown_since or time.time()),
        })
        if await self._notify(s, text):
            self.outage_notified = True

    async def _notify(self, s: Settings, text: str) -> bool:
        assert self._client is not None
        ok, detail = await telegram.send_message(self._client, s.tg_bot_token, s.tg_chat_id, text, s.tg_silent)
        if ok:
            self.add_event("Повідомлення в Telegram надіслано")
        else:
            self.add_event(f"Не вдалося надіслати повідомлення в Telegram: {detail}", "error")
        return ok

    async def send_test_telegram(self) -> tuple[bool, str]:
        s = self.store.settings
        if not (s.tg_bot_token and s.tg_chat_id):
            return False, "Не задано токен бота або ID каналу"
        assert self._client is not None
        return await telegram.send_message(
            self._client, s.tg_bot_token, s.tg_chat_id,
            "🔧 Тестове повідомлення від SvitloBot Monitor. Налаштування Telegram працюють.",
            s.tg_silent,
        )

    # ------------------------------------------------------------------
    # Діагностика налаштувань (для кнопки «Перевірити все» у панелі)
    # ------------------------------------------------------------------

    async def diagnostics(self) -> list[dict[str, Any]]:
        """Перевіряє всі налаштування. Рівні: ok, warn, error, skip."""
        s = self.store.settings
        assert self._client is not None
        items: list[dict[str, Any]] = []

        def add(key: str, title: str, level: str, detail: str, action: str | None = None) -> None:
            items.append({"key": key, "title": title, "level": level, "detail": detail, "action": action})

        # Об'єкт перевірки
        if not s.target_host:
            add("target", "Адреса об'єкта (TARGET_IP)", "error", "не задано")
        else:
            http_res, reach_res = await self._probe(s)
            reach_title = "TCP-доступність IP" if s.reachability_method == "tcp" else "Пінг IP"
            if s.device_mode == "push":
                add("esp", "Стуки від ESP32", "ok" if http_res.ok else "error", http_res.detail)
                if not s.esp_key:
                    add("esp_key", "Ключ ESP32 (ESP_KEY)", "error", "не задано — стуки не можуть бути перевірені")
            else:
                add("esp", f"Сторінка ESP32 ({s.target_host}, порти {s.esp_ports})",
                    "ok" if http_res.ok else "error", http_res.detail)
                if self.hb_last_at and time.time() - self.hb_last_at < s.push_timeout:
                    add("mode", "Режим роботи", "warn",
                        "ESP32 зараз стукає на сервер, а в панелі обрано «проброс порту» — перемкніть режим")
            add("reach", f"{reach_title} ({s.target_host})", "ok" if reach_res.ok else "error", reach_res.detail)
            if not http_res.ok and not reach_res.ok:
                add("summary", "Висновок", "warn",
                    "ні ESP32, ні IP не відповідають — перевірте адресу, проброс порту (UPnP) "
                    "та чи є інтернет на об'єкті")

        # Світлобот
        if not s.svitlobot_key:
            add("svitlobot", "Ключ Світлобота (SVITLOBOT_TOKEN)", "error", "не задано")
        elif not s.svitlobot_enabled:
            add("svitlobot", "Світлобот", "warn", "надсилання статусу вимкнено в налаштуваннях")
        elif should_ping_svitlobot(self.debouncer.confirmed, self.last_known_power, s.unknown_policy):
            ok, detail = await self.test_svitlobot()
            add("svitlobot", "Ключ Світлобота (SVITLOBOT_TOKEN)", "ok" if ok else "error",
                f"тестовий пінг: {detail}")
        else:
            reason = {
                None: "стан ще визначається",
                PowerState.OFF: "світла немає",
                PowerState.UNKNOWN: "немає зв'язку з об'єктом",
            }.get(self.debouncer.confirmed, "пінги призупинено")
            add("svitlobot", "Ключ Світлобота (SVITLOBOT_TOKEN)", "skip",
                f"зараз пінги не надсилаються ({reason}) — тестовий пінг позначив би в каналі «світло є». "
                "Перевірте вручну, якщо впевнені.", action="svitlobot")

        # Telegram
        if not s.tg_bot_token:
            add("tg_bot", "Токен бота Telegram (TELEGRAM_BOT_TOKEN)", "warn",
                "не задано — повідомлення про інтернет не надсилатимуться")
        else:
            ok, detail, bot_id = await telegram.check_bot(self._client, s.tg_bot_token)
            add("tg_bot", "Токен бота Telegram (TELEGRAM_BOT_TOKEN)", "ok" if ok else "error", detail)
            if not s.tg_chat_id:
                add("tg_chat", "Канал Telegram (SVITLOBOT_CHANNEL)", "error", "не задано ID каналу")
            elif ok:
                level, detail = await telegram.check_chat(self._client, s.tg_bot_token, s.tg_chat_id, bot_id)
                add("tg_chat", "Канал Telegram (SVITLOBOT_CHANNEL)", level, detail, action="telegram")
            if not s.tg_enabled:
                add("tg_enabled", "Повідомлення Telegram", "warn", "вимкнено в налаштуваннях")
        return items

    async def test_svitlobot(self) -> tuple[bool, str]:
        """Надсилає один пінг у Світлобот і запам'ятовує результат."""
        s = self.store.settings
        if not s.svitlobot_key:
            return False, "не задано ключ"
        assert self._client is not None
        ok, detail = await svitlobot.send_ping(self._client, s.svitlobot_url, s.svitlobot_key)
        self.svb_last_at = time.time()
        self.svb_last_ok = ok
        self.svb_last_detail = detail
        return ok, detail

    # ------------------------------------------------------------------
    # Світлобот
    # ------------------------------------------------------------------

    async def run_svitlobot_ping(self) -> None:
        s = self.store.settings
        if not s.svitlobot_enabled or not s.svitlobot_key:
            self.svb_active = False
            return
        self.svb_active = should_ping_svitlobot(self.debouncer.confirmed, self.last_known_power, s.unknown_policy)
        if not self.svb_active:
            return
        assert self._client is not None
        ok, detail = await svitlobot.send_ping(self._client, s.svitlobot_url, s.svitlobot_key)
        self.svb_last_at = time.time()
        self.svb_last_ok = ok
        self.svb_last_detail = detail
        if ok:
            if self.svb_consecutive_errors:
                self.add_event("Пінги до Світлобота знову проходять")
            self.svb_consecutive_errors = 0
        else:
            self.svb_consecutive_errors += 1
            # Не засмічуємо журнал: пишемо першу помилку та кожну десяту.
            if self.svb_consecutive_errors == 1 or self.svb_consecutive_errors % 10 == 0:
                self.add_event(f"Помилка пінгу Світлобота: {detail}", "error")

    # ------------------------------------------------------------------
    # Стан для веб-інтерфейсу
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        s = self.store.settings
        confirmed = self.debouncer.confirmed
        return {
            "version": __version__,
            "configured": s.monitoring_ready,
            "state": confirmed.value if confirmed else None,
            "state_label": _label(confirmed) if confirmed else "визначається…",
            "state_since": _fmt_ts(self.state_changed_at),
            "last_known_power": self.last_known_power.value if self.last_known_power else None,
            "pending": {
                "state": self.debouncer.candidate.value if self.debouncer.candidate else None,
                "count": self.debouncer.count,
                "needed": s.confirm_count,
            },
            "last_check": {
                "time": _fmt_ts(self.last_check_at),
                "observed": self.last_observed.value if self.last_observed else None,
                "http": self.last_http.to_dict() if self.last_http else None,
                "reach": self.last_reach.to_dict() if self.last_reach else None,
                "reach_method": s.reachability_method,
            },
            "device": {
                "mode": s.device_mode,
                "good_port": self.esp_good_port,
                "hb_last_time": _fmt_ts(self.hb_last_at),
                "hb_last_ip": self.hb_last_ip,
                "hb_info": self.hb_last_info,
                "hb_reject_time": _fmt_ts(self.hb_reject_at),
                "hb_reject_reason": self.hb_reject_reason,
            },
            "svitlobot": {
                "enabled": s.svitlobot_enabled and bool(s.svitlobot_key),
                "switch": s.svitlobot_enabled,
                "key_set": bool(s.svitlobot_key),
                "active": self.svb_active,
                "last_time": _fmt_ts(self.svb_last_at),
                "last_ok": self.svb_last_ok,
                "last_detail": self.svb_last_detail,
            },
            "telegram": {
                "ready": s.telegram_ready,
                "switch": s.tg_enabled,
                "configured": bool(s.tg_bot_token and s.tg_chat_id),
                "outage_notified": self.outage_notified,
            },
            "events": list(reversed(self.events))[:50],
        }
