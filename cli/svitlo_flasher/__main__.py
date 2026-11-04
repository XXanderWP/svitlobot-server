"""Дозволяє запуск через `python -m svitlo_flasher`."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
