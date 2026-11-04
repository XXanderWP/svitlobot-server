#!/usr/bin/env python3
"""Збирає прошивку PlatformIO і створює об'єднаний образ для svitlo-flasher.

Використання:  python cli/scripts/build_firmware.py
Потрібно:      PlatformIO (pip install platformio) та esptool.
Результат:     cli/svitlo_flasher/firmware/svitlobot-esp32.bin (запис за адресою 0x0)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIRMWARE_DIR = ROOT / "firmware"
ENV = "esp32dev"
BUILD_DIR = FIRMWARE_DIR / ".pio" / "build" / ENV
OUTPUT = ROOT / "cli" / "svitlo_flasher" / "firmware" / "svitlobot-esp32.bin"


def find_pio() -> list[str]:
    for name in ("pio", "platformio"):
        path = shutil.which(name)
        if path:
            return [path]
    return [sys.executable, "-m", "platformio"]


def find_boot_app0() -> Path:
    core_dir = Path(os.environ.get("PLATFORMIO_CORE_DIR", Path.home() / ".platformio"))
    candidate = core_dir / "packages" / "framework-arduinoespressif32" / "tools" / "partitions" / "boot_app0.bin"
    if not candidate.is_file():
        sys.exit(f"Не знайдено boot_app0.bin: {candidate}")
    return candidate


def main() -> None:
    print("• Збирання прошивки PlatformIO...")
    subprocess.run(find_pio() + ["run", "-e", ENV], cwd=FIRMWARE_DIR, check=True)

    parts = {
        "0x1000": BUILD_DIR / "bootloader.bin",
        "0x8000": BUILD_DIR / "partitions.bin",
        "0xe000": find_boot_app0(),
        "0x10000": BUILD_DIR / "firmware.bin",
    }
    for path in parts.values():
        if not path.is_file():
            sys.exit(f"Не знайдено файл збирання: {path}")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "esptool", "--chip", "esp32", "merge_bin",
           "-o", str(OUTPUT), "--flash_mode", "dio", "--flash_size", "4MB"]
    for offset, path in parts.items():
        cmd += [offset, str(path)]
    print("• Об'єднання образу...")
    subprocess.run(cmd, check=True)
    print(f"✔ Готово: {OUTPUT}")


if __name__ == "__main__":
    main()
