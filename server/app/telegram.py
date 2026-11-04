"""Робота з Telegram Bot API: надсилання повідомлень і перевірка налаштувань.

Увага: токен бота є частиною URL, тому текст винятків httpx (де може бути URL)
ніколи не потрапляє в лог чи відповідь — лише назва класу помилки.
"""

from __future__ import annotations

from typing import Any

import httpx

API = "https://api.telegram.org/bot{token}/{method}"


async def _call(client: httpx.AsyncClient, token: str, method: str,
                payload: dict[str, Any]) -> tuple[bool, Any]:
    """Повертає (успіх, result або текст помилки)."""
    try:
        resp = await client.post(API.format(token=token, method=method), json=payload, timeout=15)
    except httpx.HTTPError as exc:
        return False, f"помилка з'єднання з Telegram: {exc.__class__.__name__}"
    try:
        data = resp.json()
    except ValueError:
        return False, f"некоректна відповідь Telegram (код {resp.status_code})"
    if data.get("ok"):
        return True, data.get("result")
    description = data.get("description", f"код {resp.status_code}")
    if resp.status_code == 401:
        return False, "невірний токен бота (Unauthorized)"
    return False, f"Telegram відхилив запит: {description}"


async def send_message(client: httpx.AsyncClient, token: str, chat_id: str, text: str,
                       silent: bool = True) -> tuple[bool, str]:
    """Повертає (успіх, опис помилки або «OK»)."""
    ok, result = await _call(client, token, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "disable_notification": silent,
        "disable_web_page_preview": True,
    })
    return ok, "OK" if ok else result


async def check_bot(client: httpx.AsyncClient, token: str) -> tuple[bool, str, int | None]:
    """Перевіряє токен бота. Повертає (успіх, опис, id бота)."""
    ok, result = await _call(client, token, "getMe", {})
    if not ok:
        return False, result, None
    return True, f"бот @{result.get('username')} ({result.get('first_name', '')})", result.get("id")


async def check_chat(client: httpx.AsyncClient, token: str, chat_id: str,
                     bot_id: int | None) -> tuple[str, str]:
    """Перевіряє канал і права бота в ньому. Повертає (рівень ok|warn|error, опис)."""
    ok, chat = await _call(client, token, "getChat", {"chat_id": chat_id})
    if not ok:
        return "error", f"канал недоступний для бота: {chat}"
    title = chat.get("title") or chat.get("username") or str(chat.get("id"))
    kind = chat.get("type")
    if bot_id is None:
        return "ok", f"«{title}» ({kind})"

    ok, member = await _call(client, token, "getChatMember", {"chat_id": chat_id, "user_id": bot_id})
    if not ok:
        return "warn", f"«{title}»: не вдалося перевірити права бота ({member})"
    status = member.get("status")
    if kind == "channel":
        if status == "creator" or (status == "administrator" and member.get("can_post_messages")):
            return "ok", f"«{title}»: бот є адміністратором з правом публікації"
        if status == "administrator":
            return "error", f"«{title}»: бот адміністратор, але без права публікувати повідомлення"
        return "error", f"«{title}»: бот не є адміністратором каналу"
    if status in ("creator", "administrator", "member"):
        return "ok", f"«{title}» ({kind}): бот є учасником"
    return "error", f"«{title}» ({kind}): бот не є учасником чату"
