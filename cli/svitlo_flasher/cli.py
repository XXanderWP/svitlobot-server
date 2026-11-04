"""Командний інтерфейс svitlo-flasher."""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import sys
from pathlib import Path
from typing import Optional

from . import __version__
from .device import DeviceConfig, DeviceError, Session, list_serial_ports

FIRMWARE_NAME = "svitlobot-esp32.bin"
DEFAULT_FLASH_BAUD = 460800


# ---------------------------------------------------------------------------
# Допоміжні функції виводу
# ---------------------------------------------------------------------------

def info(msg: str) -> None:
    print(f"• {msg}")


def ok(msg: str) -> None:
    print(f"✔ {msg}")


def warn(msg: str) -> None:
    print(f"⚠ {msg}", file=sys.stderr)


def fail(msg: str) -> int:
    print(f"✖ {msg}", file=sys.stderr)
    return 1


def device_log(line: str) -> None:
    print(f"  [ESP32] {line}")


def default_firmware_path() -> Path:
    # Працює і для звичайного встановлення, і для зібраного PyInstaller-файлу.
    return Path(__file__).resolve().parent / "firmware" / FIRMWARE_NAME


# ---------------------------------------------------------------------------
# Вибір порту та введення даних
# ---------------------------------------------------------------------------

def resolve_port(requested: Optional[str]) -> str:
    if requested:
        return requested
    ports = list_serial_ports()
    likely = [p for p in ports if p.likely_esp]
    if len(likely) == 1:
        info(f"Автоматично обрано порт {likely[0].device} ({likely[0].description})")
        return likely[0].device
    if not ports:
        raise DeviceError("не знайдено жодного послідовного порту. Підключіть ESP32 кабелем USB з передачею даних.")
    if not sys.stdin.isatty():
        raise DeviceError("знайдено кілька портів — вкажіть потрібний через --port")
    print("Доступні порти:")
    for i, p in enumerate(ports, 1):
        mark = "  ← схоже на ESP32" if p.likely_esp else ""
        print(f"  {i}. {p.device}  {p.description}{mark}")
    while True:
        answer = input("Оберіть номер порту: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(ports):
            return ports[int(answer) - 1].device
        print("Невірний вибір, спробуйте ще раз.")


def ask(prompt: str, secret: bool = False) -> str:
    if not sys.stdin.isatty():
        raise DeviceError(f"не задано обов'язковий параметр: {prompt}")
    return getpass.getpass(f"{prompt}: ") if secret else input(f"{prompt}: ").strip()


def validate_ip(value: str, name: str) -> str:
    if not value:
        return ""
    try:
        return str(ipaddress.IPv4Address(value))
    except ValueError as exc:
        raise DeviceError(f"некоректна адреса {name}: {value}") from exc


def build_config(args: argparse.Namespace) -> DeviceConfig:
    ssid = args.ssid or ask("Назва Wi-Fi мережі (SSID)")
    if not ssid:
        raise DeviceError("SSID не може бути порожнім")
    if args.password is not None:
        password = args.password
    else:
        password = ask("Пароль Wi-Fi (порожньо для відкритої мережі)", secret=True)
    if password and len(password) < 8:
        raise DeviceError("пароль WPA має містити щонайменше 8 символів")
    if not 1 <= args.http_port <= 65535:
        raise DeviceError("порт має бути в діапазоні 1..65535")

    cfg = DeviceConfig(
        ssid=ssid,
        password=password,
        port=args.http_port,
        key=args.key or "",
        hostname=args.hostname,
        ip=validate_ip(args.static_ip or "", "--static-ip"),
        gateway=validate_ip(args.gateway or "", "--gateway"),
        subnet=validate_ip(args.subnet or "", "--subnet"),
        dns=validate_ip(args.dns or "", "--dns"),
    )
    if cfg.ip and not (cfg.gateway and cfg.subnet):
        raise DeviceError("для статичного IP вкажіть також --gateway і --subnet")
    return cfg


# ---------------------------------------------------------------------------
# Команди
# ---------------------------------------------------------------------------

def cmd_ports(_: argparse.Namespace) -> int:
    ports = list_serial_ports()
    if not ports:
        warn("Послідовних портів не знайдено.")
        return 0
    for p in ports:
        mark = "  ← схоже на ESP32" if p.likely_esp else ""
        print(f"{p.device}  {p.description}{mark}")
    return 0


def run_esptool(argv: list[str]) -> None:
    import esptool  # імпорт тут, щоб інші команди працювали швидше

    info("Запуск esptool: " + " ".join(argv))
    try:
        esptool.main(argv)
    except SystemExit as exc:
        if exc.code not in (0, None):
            raise DeviceError(f"esptool завершився з кодом {exc.code}") from exc
    except Exception as exc:  # esptool кидає власні винятки (FatalError тощо)
        raise DeviceError(f"помилка прошивки: {exc}") from exc


def configure_device(port: str, cfg: DeviceConfig, reset_first: bool) -> int:
    with Session(port, on_log=device_log) as dev:
        if reset_first:
            dev.reset()
        info("Встановлення зв'язку з прошивкою...")
        dev.handshake()
        info("Передача налаштувань...")
        dev.apply_config(cfg)
        ok("Налаштування збережено, пристрій перезавантажується")
        info(f"Очікування підключення до Wi-Fi «{cfg.ssid}»...")
        ip = dev.wait_for_ip()

    if not ip:
        warn("Пристрій не підключився до Wi-Fi за відведений час.")
        warn("Перевірте SSID і пароль (ESP32 підтримує лише 2,4 ГГц) та запустіть configure ще раз.")
        return 1

    ok(f"ESP32 у мережі: {ip}")
    url = f"http://{ip}:{cfg.port}/status"
    if cfg.key:
        url += "?key=<ваш ключ>"
    print()
    print("Що далі:")
    print(f"  1. Перевірте сторінку в локальній мережі: {url}")
    print(f"  2. Закріпіть за ESP32 адресу {ip} у DHCP роутера (або використайте --static-ip).")
    print(f"  3. Пробросьте на роутері зовнішній порт → {ip}:{cfg.port} (TCP).")
    print("  4. Вкажіть зовнішній IP/домен і порт у веб-інтерфейсі сервісу на VPS.")
    return 0


def cmd_flash(args: argparse.Namespace) -> int:
    firmware = Path(args.firmware) if args.firmware else default_firmware_path()
    if not firmware.is_file():
        return fail(
            f"не знайдено образ прошивки: {firmware}\n"
            "  Завантажте його з релізу або зберіть: python cli/scripts/build_firmware.py\n"
            "  Також можна вказати шлях вручну: --firmware шлях/до/файлу.bin"
        )
    try:
        port = resolve_port(args.port)
        cfg = None if args.no_config else build_config(args)

        esptool_args = ["--chip", "esp32", "--port", port, "--baud", str(args.baud),
                        "write_flash", "-z"]
        if args.erase:
            esptool_args.append("--erase-all")
        esptool_args += ["0x0", str(firmware)]
        info(f"Прошивка {firmware.name} на {port}...")
        run_esptool(esptool_args)
        ok("Прошивку записано")

        if cfg is None:
            return 0
        return configure_device(port, cfg, reset_first=True)
    except DeviceError as exc:
        return fail(str(exc))
    except KeyboardInterrupt:
        return fail("перервано користувачем")


def cmd_configure(args: argparse.Namespace) -> int:
    try:
        port = resolve_port(args.port)
        cfg = build_config(args)
        return configure_device(port, cfg, reset_first=args.reset)
    except DeviceError as exc:
        return fail(str(exc))
    except KeyboardInterrupt:
        return fail("перервано користувачем")


def cmd_info(args: argparse.Namespace) -> int:
    try:
        port = resolve_port(args.port)
        with Session(port) as dev:
            dev.handshake()
            data = dev.get_info()
    except DeviceError as exc:
        return fail(str(exc))

    labels = [
        ("version", "Версія прошивки"),
        ("configured", "Налаштовано"),
        ("ssid", "Wi-Fi мережа"),
        ("connected", "Підключено"),
        ("ip", "IP-адреса"),
        ("static_ip", "Статичний IP"),
        ("port", "HTTP-порт"),
        ("key_set", "Ключ доступу"),
        ("hostname", "Ім'я хоста"),
        ("rssi", "Рівень сигналу, дБм"),
        ("mac", "MAC-адреса"),
    ]
    for key, label in labels:
        value = data.get(key, "")
        if isinstance(value, bool):
            value = "так" if value else "ні"
        print(f"{label:22} {value if value != '' else '—'}")
    return 0


def cmd_reset_config(args: argparse.Namespace) -> int:
    try:
        port = resolve_port(args.port)
        with Session(port, on_log=device_log) as dev:
            dev.handshake()
            dev.clear_config()
    except DeviceError as exc:
        return fail(str(exc))
    ok("Налаштування стерто, пристрій перезавантажується")
    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    try:
        port = resolve_port(args.port)
        info(f"Монітор порту {port}. Вихід — Ctrl+C.")
        with Session(port) as dev:
            if args.reset:
                dev.reset()
            while True:
                line = dev.raw_read(1.0)
                if line is not None:
                    print(line)
    except DeviceError as exc:
        return fail(str(exc))
    except KeyboardInterrupt:
        print()
        return 0


# ---------------------------------------------------------------------------
# Розбір аргументів
# ---------------------------------------------------------------------------

def add_port_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("-p", "--port", metavar="ПОРТ", help="послідовний порт (напр. /dev/ttyUSB0, /dev/cu.usbserial-0001, COM3)")


def add_config_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("налаштування ESP32")
    g.add_argument("--ssid", metavar="SSID", help="назва Wi-Fi мережі (2,4 ГГц)")
    g.add_argument("--password", metavar="ПАРОЛЬ", help="пароль Wi-Fi (якщо не вказано — буде запитано)")
    g.add_argument("--http-port", metavar="ПОРТ", type=int, default=8080, help="порт HTTP-сторінки на ESP32 (типово 8080)")
    g.add_argument("--key", metavar="КЛЮЧ", help="ключ доступу до сторінки (необов'язково, рекомендовано)")
    g.add_argument("--hostname", metavar="ІМ'Я", default="svitlobot", help="ім'я пристрою в мережі (типово svitlobot)")
    g.add_argument("--static-ip", metavar="IP", help="статична IP-адреса ESP32 (необов'язково)")
    g.add_argument("--gateway", metavar="IP", help="шлюз для статичного IP")
    g.add_argument("--subnet", metavar="МАСКА", help="маска підмережі для статичного IP, напр. 255.255.255.0")
    g.add_argument("--dns", metavar="IP", help="DNS-сервер для статичного IP (типово — шлюз)")


_ARGPARSE_UK = {
    "usage: ": "Використання: ",
    "options": "параметри",
    "optional arguments": "параметри",
    "positional arguments": "позиційні аргументи",
    "show this help message and exit": "показати довідку та вийти",
    "%(prog)s: error: %(message)s\n": "%(prog)s: помилка: %(message)s\n",
    "the following arguments are required: %s": "обов'язкові аргументи: %s",
    "unrecognized arguments: %s": "невідомі аргументи: %s",
    "invalid choice: %(value)r (choose from %(choices)s)": "невірний вибір: %(value)r (можливі: %(choices)s)",
    "invalid %(type)s value: %(value)r": "невірне значення (%(type)s): %(value)r",
    "expected one argument": "очікується одне значення",
    "argument %(argument_name)s: %(message)s": "аргумент %(argument_name)s: %(message)s",
}


def _translate_argparse() -> None:
    """Перекладає вбудовані повідомлення argparse українською."""
    original = argparse._  # type: ignore[attr-defined]
    argparse._ = lambda text: _ARGPARSE_UK.get(text, original(text))  # type: ignore[attr-defined]


_translate_argparse()


class UkrainianHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Форматувач довідки, що зберігає переноси рядків у прикладах."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="svitlo-flasher",
        description="Прошивка та налаштування ESP32 для SvitloBot Monitor.",
        epilog=(
            "Приклади:\n"
            "  svitlo-flasher ports\n"
            "  svitlo-flasher flash --ssid МояМережа --key секрет123\n"
            "  svitlo-flasher configure --port COM3 --ssid МояМережа\n"
            "  svitlo-flasher info"
        ),
        formatter_class=UkrainianHelpFormatter,
        add_help=False,
    )
    parser.add_argument("-h", "--help", action="help", help="показати довідку та вийти")
    parser.add_argument("-V", "--version", action="version", version=f"svitlo-flasher {__version__}",
                        help="показати версію та вийти")
    sub = parser.add_subparsers(dest="command", title="команди", metavar="<команда>")

    def subparser(name: str, help_text: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_text, description=help_text,
                            formatter_class=UkrainianHelpFormatter, add_help=False)
        sp.add_argument("-h", "--help", action="help", help="показати довідку та вийти")
        return sp

    p = subparser("ports", "показати доступні послідовні порти")
    p.set_defaults(func=cmd_ports)

    p = subparser("flash", "записати прошивку на ESP32 і передати налаштування Wi-Fi")
    add_port_arg(p)
    p.add_argument("--firmware", metavar="ФАЙЛ", help=f"шлях до образу прошивки (типово вбудований {FIRMWARE_NAME})")
    p.add_argument("--baud", metavar="ШВИДКІСТЬ", type=int, default=DEFAULT_FLASH_BAUD, help=f"швидкість прошивки (типово {DEFAULT_FLASH_BAUD})")
    p.add_argument("--erase", action="store_true", help="повністю стерти flash перед записом")
    p.add_argument("--no-config", action="store_true", help="лише записати прошивку, без налаштувань")
    add_config_args(p)
    p.set_defaults(func=cmd_flash)

    p = subparser("configure", "змінити налаштування вже прошитого ESP32")
    add_port_arg(p)
    p.add_argument("--reset", action="store_true", help="перезавантажити пристрій перед налаштуванням")
    add_config_args(p)
    p.set_defaults(func=cmd_configure)

    p = subparser("info", "показати поточний стан і налаштування ESP32")
    add_port_arg(p)
    p.set_defaults(func=cmd_info)

    p = subparser("reset-config", "стерти налаштування на ESP32")
    add_port_arg(p)
    p.set_defaults(func=cmd_reset_config)

    p = subparser("monitor", "показувати вивід ESP32 з послідовного порту")
    add_port_arg(p)
    p.add_argument("--reset", action="store_true", help="перезавантажити пристрій на початку")
    p.set_defaults(func=cmd_monitor)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    # У Windows консоль може бути не в UTF-8 — вмикаємо, щоб українські літери відображались.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
