"""Приймання «стуків» від ESP32 (режим push).

ESP32 надсилає `POST /hb` з JSON-тілом і заголовками:
  X-Svitlo-Ts  — час відправлення (Unix, секунди, UTC);
  X-Svitlo-Sig — HMAC-SHA256(ключ, "<ts>\\n<тіло>") у hex.

Стуки приймає окремий HTTP-сервер на власному порту (типово 8081), щоб веб-панель
можна було сховати (WEB_BIND=127.0.0.1), а порт стуків — лишити відкритим.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

MAX_CLOCK_SKEW = 300  # секунд розбіжності годинників ESP32 і сервера
MAX_BODY = 2048


@dataclass
class Verification:
    ok: bool
    reason: str
    ts: Optional[int] = None
    info: Optional[dict[str, Any]] = None


def sign(key: str, ts: str, body: bytes) -> str:
    return hmac.new(key.encode(), ts.encode() + b"\n" + body, hashlib.sha256).hexdigest()


def verify(key: str, ts_header: str | None, sig_header: str | None, body: bytes,
           now: float, last_ts: Optional[int]) -> Verification:
    """Перевіряє підпис і свіжість стуку. Повтор старого стуку відхиляється."""
    if not key:
        return Verification(False, "на сервері не задано ключ ESP32")
    if not ts_header or not sig_header:
        return Verification(False, "немає підпису")
    if not ts_header.isdigit():
        return Verification(False, "некоректний час")
    ts = int(ts_header)
    expected = sign(key, ts_header, body)
    if not hmac.compare_digest(expected, sig_header.strip().lower()):
        return Verification(False, "невірний підпис (ключ не збігається)")
    if abs(now - ts) > MAX_CLOCK_SKEW:
        return Verification(False, f"час ESP32 відрізняється від серверного на {int(abs(now - ts))} с")
    if last_ts is not None and ts <= last_ts:
        return Verification(False, "повтор попереднього стуку")
    try:
        info = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, ValueError):
        return Verification(False, "некоректне тіло запиту")
    if not isinstance(info, dict) or info.get("device") != "svitlobot-esp32":
        return Verification(False, "запит не від прошивки SvitloBot")
    return Verification(True, "OK", ts, info)


def create_app(get_key: Callable[[], str], get_last_ts: Callable[[], Optional[int]],
               on_accept: Callable[[Verification, str], None],
               on_reject: Callable[[str, str], None]) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/hb")
    async def heartbeat(request: Request) -> JSONResponse:
        client_ip = request.client.host if request.client else "?"
        body = await request.body()
        if len(body) > MAX_BODY:
            on_reject("запит завеликий", client_ip)
            return JSONResponse({"ok": False, "error": "запит завеликий"}, status_code=413)
        result = verify(get_key(), request.headers.get("x-svitlo-ts"), request.headers.get("x-svitlo-sig"),
                        body, time.time(), get_last_ts())
        if not result.ok:
            on_reject(result.reason, client_ip)
            return JSONResponse({"ok": False, "error": result.reason}, status_code=401)
        on_accept(result, client_ip)
        return JSONResponse({"ok": True})

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
