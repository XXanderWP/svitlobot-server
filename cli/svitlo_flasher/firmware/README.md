# Образ прошивки

Сюди кладеться об'єднаний образ `svitlobot-esp32.bin` (записується за адресою `0x0`).

Він не зберігається в git. Отримати його можна так:

- завантажити з релізу на GitHub (його збирає CI);
- зібрати локально: `python cli/scripts/build_firmware.py` (потрібен PlatformIO).

Або вкажіть шлях вручну: `svitlo-flasher flash --firmware шлях/до/файлу.bin`.
