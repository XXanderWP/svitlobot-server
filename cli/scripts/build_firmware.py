#!/usr/bin/env python3
"""Збирає прошивку PlatformIO і створює об'єднані образи для svitlo-flasher.

Використання:  python cli/scripts/build_firmware.py
Потрібно:      PlatformIO (pip install platformio) та esptool.
Результат:     cli/svitlo_flasher/firmware/svitlobot-<чип>.bin (запис за адресою 0x0)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIRMWARE_DIR = ROOT / "firmware"
OUTPUT_DIR = ROOT / "cli" / "svitlo_flasher" / "firmware"

# Середовище PlatformIO → (чип esptool, адреса завантажувача, режим flash, розмір flash, файл образу)
TARGETS = {
    "esp32dev": ("esp32", "0x1000", "dio", "4MB", "svitlobot-esp32.bin"),
    "esp32s3": ("esp32s3", "0x0", "dio", "4MB", "svitlobot-esp32s3.bin"),
}


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
    envs = sys.argv[1:] or list(TARGETS)
    for env in envs:
        if env not in TARGETS:
            sys.exit(f"Невідоме середовище: {env}. Доступні: {', '.join(TARGETS)}")

    print(f"• Збирання прошивки PlatformIO: {', '.join(envs)}...")
    cmd = find_pio() + ["run"]
    for env in envs:
        cmd += ["-e", env]
    subprocess.run(cmd, cwd=FIRMWARE_DIR, check=True)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    boot_app0 = find_boot_app0()
    for env in envs:
        chip, bootloader_addr, flash_mode, flash_size, name = TARGETS[env]
        build_dir = FIRMWARE_DIR / ".pio" / "build" / env
        parts = {
            bootloader_addr: build_dir / "bootloader.bin",
            "0x8000": build_dir / "partitions.bin",
            "0xe000": boot_app0,
            "0x10000": build_dir / "firmware.bin",
        }
        for path in parts.values():
            if not path.is_file():
                sys.exit(f"Не знайдено файл збирання: {path}")
        output = OUTPUT_DIR / name
        merge = [sys.executable, "-m", "esptool", "--chip", chip, "merge_bin", "-o", str(output),
                 "--flash_mode", flash_mode, "--flash_size", flash_size]
        for offset, path in parts.items():
            merge += [offset, str(path)]
        print(f"• Об'єднання образу {name}...")
        subprocess.run(merge, check=True, stdout=subprocess.DEVNULL)
        print(f"✔ Готово: {output}")


if __name__ == "__main__":
    main()
