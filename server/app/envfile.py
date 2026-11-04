"""Читання та запис файлів у форматі .env (KEY=value).

Підтримується підмножина формату, сумісна з Docker Compose:
- `KEY=value` — значення до кінця рядка (коментар після ` #` відкидається);
- `KEY='value'` — буквальне значення без екранування;
- `KEY="value"` — екрановані `\\n`, `\\r`, `\\t`, `\\"`, `\\\\`;
- порожні рядки, коментарі `#` та префікс `export ` ігноруються.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}


def _parse_double_quoted(raw: str) -> str:
    out = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            out.append(_ESCAPES.get(nxt, "\\" + nxt))
            i += 2
            continue
        if ch == '"':
            break
        out.append(ch)
        i += 1
    return "".join(out)


def parse_env(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _KEY_RE.match(key):
            continue
        value = value.strip()
        if value.startswith('"'):
            value = _parse_double_quoted(value[1:])
        elif value.startswith("'"):
            end = value.find("'", 1)
            value = value[1:end] if end != -1 else value[1:]
        else:
            hash_pos = value.find(" #")
            if hash_pos != -1:
                value = value[:hash_pos].rstrip()
        result[key] = value
    return result


def read_env_file(path: Path) -> dict[str, str]:
    try:
        return parse_env(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def quote_value(value: str) -> str:
    """Безпечне подання значення: у лапках, з екрануванням спецсимволів."""
    if "'" not in value and "\n" not in value and "\r" not in value:
        return f"'{value}'"
    escaped = (value.replace("\\", "\\\\").replace('"', '\\"')
               .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
    return f'"{escaped}"'


def write_env_file(path: Path, values: dict[str, str], header: str = "") -> None:
    """Атомарно записує файл з правами 600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# {line}" if line else "#" for line in header.splitlines()]
    lines += [f"{key}={quote_value(val)}" for key, val in sorted(values.items())]
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
