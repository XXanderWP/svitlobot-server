"""Веб-застосунок: REST API та веб-інтерфейс налаштувань."""

from __future__ import annotations

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


@asynccontextmanager
async def lifespan(_: FastAPI):
    await monitor.start()
    try:
        yield
    finally:
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
    return monitor.status()


@app.get("/api/config", dependencies=[Depends(require_auth)])
async def api_get_config() -> dict[str, Any]:
    return store.public_dict()


@app.put("/api/config", dependencies=[Depends(require_auth)])
async def api_put_config(changes: dict[str, Any] = Body(...)) -> JSONResponse:
    try:
        settings = store.update(changes)
    except ValidationError as exc:
        return JSONResponse({"ok": False, "error": format_validation_error(exc)}, status_code=422)
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
    return {"ok": True, "status": monitor.status()}


@app.post("/api/test/telegram", dependencies=[Depends(require_auth)])
async def api_test_telegram() -> dict[str, Any]:
    ok, detail = await monitor.send_test_telegram()
    return {"ok": ok, "detail": detail}
