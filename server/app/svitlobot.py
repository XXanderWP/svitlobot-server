"""Клієнт API Світлобота."""

from __future__ import annotations

from urllib.parse import quote

import httpx


async def send_ping(client: httpx.AsyncClient, url_template: str, key: str) -> tuple[bool, str]:
    """Надсилає пінг «світло є». Повертає (успіх, опис)."""
    url = url_template.replace("{key}", quote(key, safe=""))
    try:
        resp = await client.get(url, timeout=15)
    except httpx.HTTPError as exc:
        return False, f"помилка з'єднання: {exc.__class__.__name__}"
    if 200 <= resp.status_code < 300:
        return True, f"OK ({resp.status_code})"
    return False, f"код відповіді {resp.status_code}: {resp.text[:200]}"
