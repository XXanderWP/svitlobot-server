"""Веб-застосунок: REST API та веб-інтерфейс налаштувань."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import ValidationError
import uvicorn

from . import heartbeat
from .config import DATA_DIR, ConfigStore, format_validation_error
from .monitor import Monitor

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("svitlobot")

WEB_DIR = Path(__file__).resolve().parent / "web"

store = ConfigStore()
monitor = Monitor(store, DATA_DIR / "state.json")

# Дані для входу в панель беруться лише з .env (або оточення) і в панелі не змінюються.
PANEL_USERNAME = store.env_value("PANEL_USERNAME", "admin") or "admin"
PANEL_PASSWORD = store.env_value("PANEL_PASSWORD", "")
if not PANEL_PASSWORD:
    PANEL_PASSWORD = secrets.token_urlsafe(12)
    log.warning(
        "PANEL_PASSWORD не задано. Згенеровано тимчасовий пароль для входу: "
        "користувач «%s», пароль «%s». Задайте власний пароль у файлі .env.",
        PANEL_USERNAME, PANEL_PASSWORD,
    )


HEARTBEAT_LISTEN_PORT = int(os.environ.get("HEARTBEAT_LISTEN_PORT", "8081"))
# Зовнішній порт стуків (з compose.env) — лише для підказки у веб-панелі.
HEARTBEAT_PUBLIC_PORT = int(os.environ.get("HEARTBEAT_PUBLIC_PORT", str(HEARTBEAT_LISTEN_PORT)))


class _EmbeddedServer(uvicorn.Server):
    """Другий uvicorn-сервер у тому ж процесі: не перехоплює сигнали головного сервера."""

    @contextlib.contextmanager
    def capture_signals(self):  # uvicorn ≥ 0.29
        yield

    def install_signal_handlers(self) -> None:  # старіші версії uvicorn
        pass


async def _run_heartbeat_server(server: _EmbeddedServer) -> None:
    try:
        await server.serve()
    except (SystemExit, OSError) as exc:
        log.error("Не вдалося запустити сервер стуків на порту %s: %s", HEARTBEAT_LISTEN_PORT, exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await monitor.start()
    hb_server = None
    hb_task = None
    if HEARTBEAT_LISTEN_PORT:
        hb_app = heartbeat.create_app(monitor.heartbeat_key, monitor.heartbeat_last_ts,
                                      monitor.accept_heartbeat, monitor.reject_heartbeat)
        hb_server = _EmbeddedServer(uvicorn.Config(
            hb_app, host="0.0.0.0", port=HEARTBEAT_LISTEN_PORT, lifespan="off",
            access_log=False, log_level="warning",
        ))
        hb_task = asyncio.create_task(_run_heartbeat_server(hb_server))
        log.info("Сервер стуків слухає порт %s", HEARTBEAT_LISTEN_PORT)
    try:
        yield
    finally:
        if hb_server and hb_task:
            hb_server.should_exit = True
            await hb_task
        await monitor.stop()


app = FastAPI(title="SvitloBot Monitor", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
security = HTTPBasic(realm="SvitloBot Monitor")


def require_auth(credentials: HTTPBasicCredentials = Depends(security)) -> None:
    user_ok = secrets.compare_digest(credentials.username.encode(), PANEL_USERNAME.encode())
    pass_ok = secrets.compare_digest(credentials.password.encode(), PANEL_PASSWORD.encode())
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Невірний логін або пароль",
            headers={"WWW-Authenticate": 'Basic realm="SvitloBot Monitor"'},
        )


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """Перевірка працездатності для Docker (без авторизації, без даних)."""
    return {"status": "ok"}


@app.get("/", dependencies=[Depends(require_auth)], include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/status", dependencies=[Depends(require_auth)])
async def api_status() -> dict[str, Any]:
    data = monitor.status()
    data["device"]["hb_public_port"] = HEARTBEAT_PUBLIC_PORT
    return data


@app.get("/api/config", dependencies=[Depends(require_auth)])
async def api_get_config() -> dict[str, Any]:
    return store.public_dict()


@app.put("/api/config", dependencies=[Depends(require_auth)])
async def api_put_config(changes: dict[str, Any] = Body(...)) -> JSONResponse:
    before = store.settings
    try:
        settings = store.update(changes)
    except ValidationError as exc:
        return JSONResponse({"ok": False, "error": format_validation_error(exc)}, status_code=422)
    if settings.svitlobot_enabled != before.svitlobot_enabled:
        monitor.add_event("Надсилання сигналів у Світлобот увімкнено" if settings.svitlobot_enabled
                          else "Надсилання сигналів у Світлобот вимкнено", "info" if settings.svitlobot_enabled else "warn")
    if settings.tg_enabled != before.tg_enabled:
        monitor.add_event("Сповіщення про інтернет у Telegram увімкнено" if settings.tg_enabled
                          else "Сповіщення про інтернет у Telegram вимкнено")
    monitor.add_event("Налаштування змінено в панелі (збережено в .env.override)")
    monitor.wake()
    return JSONResponse({"ok": True, "config": store.public_dict()})


@app.post("/api/config/reset", dependencies=[Depends(require_auth)])
async def api_reset_config(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Скидає зміни панелі: для полів зі списку `fields` або всі, якщо список не передано."""
    fields = body.get("fields")
    if fields is not None and not isinstance(fields, list):
        raise HTTPException(status_code=400, detail="fields має бути списком")
    store.reset(fields)
    monitor.add_event("Зміни панелі скинуто — діють значення з .env" if fields is None
                      else "Скинуто зміни панелі для окремих полів")
    monitor.wake()
    return {"ok": True, "config": store.public_dict()}


@app.post("/api/diagnostics", dependencies=[Depends(require_auth)])
async def api_diagnostics() -> dict[str, Any]:
    return {"ok": True, "items": await monitor.diagnostics()}


@app.post("/api/test/svitlobot", dependencies=[Depends(require_auth)])
async def api_test_svitlobot() -> dict[str, Any]:
    ok, detail = await monitor.test_svitlobot()
    return {"ok": ok, "detail": detail}


@app.post("/api/check-now", dependencies=[Depends(require_auth)])
async def api_check_now() -> dict[str, Any]:
    if not store.settings.monitoring_ready:
        return {"ok": False, "error": "Не задано адресу об'єкта"}
    await monitor.run_check()
    return {"ok": True, "status": await api_status()}


@app.post("/api/test/telegram", dependencies=[Depends(require_auth)])
async def api_test_telegram() -> dict[str, Any]:
    ok, detail = await monitor.send_test_telegram()
    return {"ok": ok, "detail": detail}
