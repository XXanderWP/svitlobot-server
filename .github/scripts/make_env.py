#!/usr/bin/env python3
"""Формує файл .env для сервера із секретів GitHub Actions.

Значення секретів читаються зі змінних оточення кроку і НІКОЛИ не виводяться в лог:
скрипт друкує лише назви змінних. Файл створюється з правами 600.

Поруч створюється compose.env лише з несекретними параметрами для Docker Compose
(WEB_PORT, WEB_BIND). Compose запускається з ним замість .env, бо інакше він сам розбирав би
.env для підстановки змінних і міг би вивести в лог фрагмент секрету (напр. частину пароля
після символу `$` у попередженні «variable is not set»).

Використання: python3 .github/scripts/make_env.py <шлях до файлу>
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Використовуємо той самий формат лапок, що й застосунок, щоб значення читались однаково.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))
from app.envfile import quote_value  # noqa: E402

REQUIRED = ["PANEL_USERNAME", "PANEL_PASSWORD", "SVITLOBOT_TOKEN", "TARGET_IP"]
OPTIONAL = ["TELEGRAM_BOT_TOKEN", "SVITLOBOT_CHANNEL", "ESP_PORT", "ESP_KEY"]
# Несекретні параметри контейнера — лише для compose.env.
COMPOSE_ONLY = ["WEB_PORT", "WEB_BIND"]


def write_private(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def main() -> int:
    if len(sys.argv) != 2:
        print("Використання: make_env.py <шлях до файлу>", file=sys.stderr)
        return 2

    missing = [name for name in REQUIRED if not os.environ.get(name, "").strip()]
    if missing:
        print(f"::error::Не задано обов'язкові секрети репозиторію: {', '.join(missing)}")
        return 1

    tg = [name for name in ("TELEGRAM_BOT_TOKEN", "SVITLOBOT_CHANNEL") if os.environ.get(name, "").strip()]
    if len(tg) == 1:
        print("::warning::Для повідомлень у Telegram потрібні обидва секрети: TELEGRAM_BOT_TOKEN і SVITLOBOT_CHANNEL")

    lines = [
        "# Файл створено автоматично GitHub Action під час деплою — не редагуйте вручну,",
        "# його буде перезаписано. Зміни робіть у веб-панелі (вони йдуть у data/.env.override)",
        "# або в секретах репозиторію.",
    ]
    written = []
    for name in REQUIRED + OPTIONAL:
        value = os.environ.get(name, "").strip()
        if not value:
            continue
        lines.append(f"{name}={quote_value(value)}")
        written.append(name)

    path = Path(sys.argv[1])
    write_private(path, lines)

    compose_lines = ["# Параметри для Docker Compose (без секретів). Створено GitHub Action."]
    for name in COMPOSE_ONLY:
        value = os.environ.get(name, "").strip()
        if value:
            compose_lines.append(f"{name}={quote_value(value)}")
    write_private(path.with_name("compose.env"), compose_lines)

    print(f"✔ Сформовано .env, змінні: {', '.join(written)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
