"""Перевірки: HTTP-сторінка ESP32 та доступність IP (ICMP або TCP)."""

from __future__ import annotations

import asyncio
import re
import shutil
import time
from dataclasses import asdict, dataclass, field

import httpx

DEVICE_ID = "svitlobot-esp32"
_PING_TIME_RE = re.compile(r"time[=<]([\d.]+)\s*ms")


@dataclass
class CheckResult:
    ok: bool
    latency_ms: float | None = None
    detail: str = ""
    info: dict | None = field(default=None, repr=False)  # JSON-відповідь ESP32 (лише при успіху)

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("info", None)
        return data


def _host_for_url(host: str) -> str:
    # IPv6-адресу в URL треба брати в квадратні дужки.
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


async def check_http(client: httpx.AsyncClient, host: str, port: int, path: str,
                     key: str, timeout: float, upstream: bool = False) -> CheckResult:
    """Звертається до сторінки ESP32. Успіх — лише якщо відповіла саме наша прошивка.

    upstream=True повідомляє ESP32 (заголовок X-Svitlo-Upstream), що VPS сам пінгує Світлобот,
    і резервний пінг з ESP32 не потрібен — так канал не отримує зайвих запитів (HTTP 429).
    """
    url = f"http://{_host_for_url(host)}:{port}{path}"
    headers = {"X-Svitlo-Key": key} if key else {}
    headers["X-Svitlo-Upstream"] = "1" if upstream else "0"
    started = time.perf_counter()
    try:
        resp = await client.get(url, headers=headers, timeout=timeout)
    except httpx.TimeoutException:
        return CheckResult(False, None, "тайм-аут")
    except httpx.HTTPError as exc:
        return CheckResult(False, None, f"помилка з'єднання: {exc.__class__.__name__}")
    latency = round((time.perf_counter() - started) * 1000, 1)

    if resp.status_code == 403:
        return CheckResult(False, latency, "відмовлено в доступі (невірний ключ ESP32)")
    if resp.status_code != 200:
        return CheckResult(False, latency, f"неочікуваний код відповіді {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        return CheckResult(False, latency, "відповідь не схожа на прошивку SvitloBot (не JSON)")
    if not isinstance(data, dict) or data.get("device") != DEVICE_ID:
        return CheckResult(False, latency, "відповідь не схожа на прошивку SvitloBot")
    uptime = data.get("uptime_s")
    detail = "сторінка відповідає"
    if isinstance(uptime, int):
        detail += f", аптайм ESP32 {uptime // 3600} год {uptime % 3600 // 60} хв"
    backup = data.get("svitlobot")
    if isinstance(backup, dict) and backup.get("enabled", True):
        if backup.get("active") is False:
            detail += "; резервний пінг ESP32 чекає (VPS пінгує сам)"
        else:
            detail += "; резервний пінг ESP32: " + ("працює" if backup.get("ok") else str(backup.get("status", "помилка")))
    return CheckResult(True, latency, detail, info=data)


async def check_icmp(host: str, timeout: float) -> CheckResult:
    """ICMP-пінг через системну утиліту ping (iputils у контейнері)."""
    if not shutil.which("ping"):
        return CheckResult(False, None, "утиліта ping недоступна")
    wait = str(max(1, int(round(timeout))))
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", wait, host,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout + 3)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return CheckResult(False, None, "тайм-аут")
    except OSError as exc:
        return CheckResult(False, None, f"не вдалося запустити ping: {exc}")

    if proc.returncode == 0:
        match = _PING_TIME_RE.search(stdout.decode(errors="replace"))
        latency = float(match.group(1)) if match else None
        return CheckResult(True, latency, "пінг проходить")
    err = stderr.decode(errors="replace").strip()
    if "unknown host" in err.lower() or "name or service not known" in err.lower():
        return CheckResult(False, None, "не вдалося визначити адресу (DNS)")
    return CheckResult(False, None, "немає відповіді на пінг")


async def check_tcp(host: str, port: int, timeout: float) -> CheckResult:
    """Альтернатива ICMP: спроба TCP-з'єднання з портом роутера."""
    started = time.perf_counter()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except asyncio.TimeoutError:
        return CheckResult(False, None, "тайм-аут")
    except ConnectionRefusedError:
        # Відмова у з'єднанні означає, що хост живий і відповідає.
        latency = round((time.perf_counter() - started) * 1000, 1)
        return CheckResult(True, latency, f"хост відповідає (порт {port} закритий)")
    except OSError as exc:
        return CheckResult(False, None, f"недоступний: {exc.strerror or exc.__class__.__name__}")
    latency = round((time.perf_counter() - started) * 1000, 1)
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    return CheckResult(True, latency, f"порт {port} відкритий")


async def check_http_ports(client: httpx.AsyncClient, host: str, ports: list[int], preferred: int | None,
                           path: str, key: str, timeout: float,
                           upstream: bool = False) -> tuple[CheckResult, int | None]:
    """Перевіряє сторінку ESP32 на кількох зовнішніх портах одночасно.

    ESP32 відкриває через UPnP перший вільний порт зі списку, тому сервер пробує всі.
    Повертає результат і порт, на якому відповіла прошивка (успіх на `preferred` має пріоритет).
    """
    order = sorted(ports, key=lambda p: p != preferred)
    results = await asyncio.gather(*(check_http(client, host, p, path, key, timeout, upstream) for p in order))
    for port, res in zip(order, results):
        if res.ok:
            res.detail = f"порт {port}: {res.detail}"
            return res, port
    # Найінформативніша помилка: «невірний ключ» або «не схоже на прошивку» важливіші за тайм-аут.
    def weight(r: CheckResult) -> int:
        return 0 if r.latency_ms is not None else 1
    port, res = min(zip(order, results), key=lambda pr: weight(pr[1]))
    if len(order) > 1 and res.latency_ms is None:
        res.detail = f"жоден із портів {','.join(map(str, order))} не відповідає ({res.detail})"
    else:
        res.detail = f"порт {port}: {res.detail}"
    return res, None
