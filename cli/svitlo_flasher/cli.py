"""Командний інтерфейс svitlo-flasher."""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import secrets
import sys
from pathlib import Path
from typing import Optional

from . import __version__
from .device import (
    DEFAULT_PUSH_INTERVAL,
    DEFAULT_SERVER_PORT,
    DEFAULT_UPNP_PORTS,
    MODE_PUSH,
    MODE_UPNP,
    DeviceConfig,
    DeviceError,
    Session,
    list_serial_ports,
)

DEFAULT_FLASH_BAUD = 460800

# Назва чипа з esptool → (аргумент --chip, файл образу)
SUPPORTED_CHIPS = {
    "ESP32": ("esp32", "svitlobot-esp32.bin"),
    "ESP32-S3": ("esp32s3", "svitlobot-esp32s3.bin"),
}


# ---------------------------------------------------------------------------
# Допоміжні функції виводу
# ---------------------------------------------------------------------------

def info(msg: str) -> None:
    print(f"• {msg}", flush=True)


def ok(msg: str) -> None:
    print(f"✔ {msg}", flush=True)


def warn(msg: str) -> None:
    sys.stdout.flush()
    print(f"⚠ {msg}", file=sys.stderr, flush=True)


def fail(msg: str) -> int:
    sys.stdout.flush()
    print(f"✖ {msg}", file=sys.stderr, flush=True)
    return 1


def device_log(line: str) -> None:
    print(f"  [ESP32] {line}", flush=True)


def firmware_dir() -> Path:
    # Працює і для звичайного встановлення, і для зібраного PyInstaller-файлу.
    return Path(__file__).resolve().parent / "firmware"


def detect_chip(port: str) -> tuple[str, str]:
    """Визначає чип на порту. Повертає (аргумент --chip для esptool, файл образу)."""
    from esptool.cmds import detect_chip as esptool_detect

    info("Визначення типу чипа...")
    try:
        esp = esptool_detect(port)
    except Exception as exc:
        raise DeviceError(f"не вдалося зв'язатися з чипом: {exc}") from exc
    name = esp.CHIP_NAME
    try:
        esp._port.close()
    except Exception:
        pass
    if name not in SUPPORTED_CHIPS:
        raise DeviceError(f"чип {name} не підтримується (підтримуються: {', '.join(SUPPORTED_CHIPS)})")
    ok(f"Чип: {name}")
    return SUPPORTED_CHIPS[name]


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


def parse_ports(text: str) -> list[int]:
    ports = []
    for item in text.replace(" ", "").split(","):
        if not item:
            continue
        if not item.isdigit() or not 1 <= int(item) <= 65535:
            raise DeviceError(f"некоректний порт у --upnp-ports: {item}")
        if int(item) not in ports:
            ports.append(int(item))
    if not ports:
        raise DeviceError("список --upnp-ports порожній")
    if len(ports) > 10:
        raise DeviceError("у --upnp-ports не більше 10 портів")
    return ports


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

    mode = args.mode
    upnp_ports = parse_ports(args.upnp_ports)
    key = args.key or ""
    server = (args.server or "").strip()
    if mode == MODE_PUSH:
        if not server:
            server = ask("IP або домен сервера (VPS) для стуків")
        if not server:
            raise DeviceError("для режиму push потрібна адреса сервера (--server)")
        if not 1 <= args.server_port <= 65535:
            raise DeviceError("--server-port має бути в діапазоні 1..65535")
        if not 5 <= args.push_interval <= 300:
            raise DeviceError("--push-interval має бути від 5 до 300 секунд")
        if not key:
            # hex: ключ не почнеться з «-», тож його можна безпечно передати в --key.
            key = secrets.token_hex(16)
            args.generated_key = True
        elif len(key) < 16:
            raise DeviceError("для режиму push ключ має містити щонайменше 16 символів")

    cfg = DeviceConfig(
        ssid=ssid,
        password=password,
        port=args.http_port,
        key=key,
        hostname=args.hostname,
        ip=validate_ip(args.static_ip or "", "--static-ip"),
        gateway=validate_ip(args.gateway or "", "--gateway"),
        subnet=validate_ip(args.subnet or "", "--subnet"),
        dns=validate_ip(args.dns or "", "--dns"),
        mode=mode,
        upnp_ports=",".join(str(p) for p in upnp_ports),
        server=server,
        server_port=args.server_port,
        push_interval=args.push_interval,
        svitlobot_key=args.svitlobot_token.strip() if args.svitlobot_token is not None else None,
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


def configure_device(port: str, cfg: DeviceConfig, reset_first: bool, generated_key: bool = False) -> int:
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
        mode_ok, mode_text, extra = False, "", []
        if ip:
            ok(f"ESP32 у мережі: {ip}")
            if cfg.mode == MODE_UPNP:
                info("Відкриття порту на роутері через UPnP...")
            else:
                info(f"Перший стук на сервер {cfg.server}:{cfg.server_port}...")
            mode_ok, mode_text, extra = dev.wait_for_mode_result(cfg.mode)
            if cfg.svitlobot_key:
                info("Резервний пінг Світлобота з ESP32...")
                svb = dev.wait_for_svitlobot()
                if svb is None:
                    warn("ESP32 не повідомила результат пінгу Світлобота (перевірте пізніше: svitlo-flasher info)")
                elif svb[0]:
                    ok(f"Світлобот: {svb[1]}")
                else:
                    warn(f"Світлобот: {svb[1]}")
            elif cfg.svitlobot_key == "":
                info("Резервний пінг Світлобота з ESP32 вимкнено")

    if not ip:
        warn("Пристрій не підключився до Wi-Fi за відведений час.")
        warn("Перевірте SSID і пароль (ESP32 підтримує лише 2,4 ГГц) та запустіть configure ще раз.")
        return 1

    print()
    if cfg.mode == MODE_UPNP:
        return report_upnp(ip, cfg, mode_ok, mode_text, extra)
    return report_push(cfg, mode_ok, mode_text, generated_key)


def report_upnp(ip: str, cfg: DeviceConfig, success: bool, text: str, extra: list[str]) -> int:
    ports_list = cfg.upnp_ports
    if success:
        parts = text.split()
        ext_port = parts[0] if parts else "?"
        ext_ip = parts[1] if len(parts) > 1 and parts[1] != "-" else "невідомо"
        ok(f"Роутер відкрив зовнішній порт {ext_port} → {ip}:{cfg.port} (зовнішній IP: {ext_ip})")
        if extra:
            warn(f"Зовнішня адреса роутера {extra[0]} — сіра (CGNAT або подвійний NAT).")
            warn("Ззовні до ESP32 не достукатися, а пінг з VPS дійде до обладнання провайдера, а не до вашого роутера,")
            warn("тож відрізнити відсутність світла від відсутності інтернету не вийде в жодному режимі.")
            warn("Потрібен білий (публічний) IP від провайдера.")
    else:
        warn(f"UPnP не спрацював: {text}")
        warn(f"Увімкніть UPnP на роутері або пробросьте вручну один із портів {ports_list} → {ip}:{cfg.port} (TCP).")
        warn("ESP32 повторюватиме спробу кожні 5 хвилин. Альтернатива без пробросу — --mode push.")
    print()
    print("Що далі:")
    print(f"  1. Закріпіть за ESP32 адресу {ip} у DHCP роутера (або використайте --static-ip).")
    print("  2. У веб-панелі на VPS оберіть режим «Проброс порту» і вкажіть:")
    print(f"     зовнішній IP/домен роутера, порти {ports_list}" + (", ключ ESP32" if cfg.key else "") + ".")
    print(f"  3. Перевірка в локальній мережі: http://{ip}:{cfg.port}/status" + ("?key=<ключ>" if cfg.key else ""))
    return 0 if success else 1


def report_push(cfg: DeviceConfig, success: bool, text: str, generated_key: bool) -> int:
    if success:
        ok(f"Сервер {cfg.server}:{cfg.server_port} прийняв стук")
    else:
        warn(f"Стук не пройшов: {text}")
        if "підпис" in text:
            warn("Ключ у панелі сервера не збігається з ключем ESP32.")
        else:
            warn(f"Перевірте адресу сервера та чи відкрито порт {cfg.server_port} на VPS (фаєрвол).")
        warn(f"ESP32 продовжить стукати кожні {cfg.push_interval} с.")
    print()
    if generated_key:
        print("Згенеровано ключ підпису стуків (збережіть його):")
        print(f"  {cfg.key}")
        print()
    print("Що далі:")
    print("  1. У веб-панелі на VPS оберіть режим «ESP32 сам стукає на сервер».")
    print("  2. Вкажіть у панелі «Ключ ESP32» — той самий, що вище" + (" (згенерований)" if generated_key else "") + ".")
    print("     Або додайте його в секрет репозиторію ESP_KEY.")
    print("  3. Вкажіть зовнішній IP/домен роутера — його сервіс пінгуватиме, щоб відрізнити")
    print("     відсутність світла від відсутності інтернету.")
    return 0 if success else 1


def cmd_flash(args: argparse.Namespace) -> int:
    try:
        port = resolve_port(args.port)
        cfg = None if args.no_config else build_config(args)

        chip, image_name = detect_chip(port)
        firmware = Path(args.firmware) if args.firmware else firmware_dir() / image_name
        if not firmware.is_file():
            return fail(
                f"не знайдено образ прошивки: {firmware}\n"
                "  Завантажте його з релізу або зберіть: python cli/scripts/build_firmware.py\n"
                "  Також можна вказати шлях вручну: --firmware шлях/до/файлу.bin"
            )

        esptool_args = ["--chip", chip, "--port", port, "--baud", str(args.baud),
                        "write_flash", "-z"]
        if args.erase:
            esptool_args.append("--erase-all")
        esptool_args += ["0x0", str(firmware)]
        info(f"Прошивка {firmware.name} на {port}...")
        run_esptool(esptool_args)
        ok("Прошивку записано")

        if cfg is None:
            return 0
        return configure_device(port, cfg, reset_first=True, generated_key=getattr(args, "generated_key", False))
    except DeviceError as exc:
        return fail(str(exc))
    except KeyboardInterrupt:
        return fail("перервано користувачем")


def cmd_configure(args: argparse.Namespace) -> int:
    try:
        port = resolve_port(args.port)
        cfg = build_config(args)
        return configure_device(port, cfg, reset_first=args.reset, generated_key=getattr(args, "generated_key", False))
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
        ("mode", "Режим роботи"),
        ("upnp_ports", "Порти UPnP"),
        ("upnp_port", "Відкритий порт"),
        ("upnp_external_ip", "Зовнішній IP (UPnP)"),
        ("upnp_status", "Стан UPnP"),
        ("server", "Сервер"),
        ("server_port", "Порт сервера"),
        ("push_interval", "Інтервал стуку, с"),
        ("push_status", "Стан стуку"),
        ("svitlobot_set", "Резервний пінг Світлобота"),
        ("svitlobot_status", "Стан пінгу Світлобота"),
    ]
    for key, label in labels:
        if key not in data:
            continue
        value = data.get(key, "")
        if key == "mode":
            value = {"upnp": "проброс порту (UPnP)", "push": "стук на сервер"}.get(value, value)
        if isinstance(value, bool):
            value = "так" if value else "ні"
        print(f"{label:27} {value if value != '' else '—'}")
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
    g.add_argument("--key", metavar="КЛЮЧ",
                   help="ключ доступу до сторінки (upnp) або підпису стуків (push; якщо не вказано — згенерується)")
    g.add_argument("--hostname", metavar="ІМ'Я", default="svitlobot", help="ім'я пристрою в мережі (типово svitlobot)")
    g.add_argument("--static-ip", metavar="IP", help="статична IP-адреса ESP32 (необов'язково)")
    g.add_argument("--gateway", metavar="IP", help="шлюз для статичного IP")
    g.add_argument("--subnet", metavar="МАСКА", help="маска підмережі для статичного IP, напр. 255.255.255.0")
    g.add_argument("--dns", metavar="IP", help="DNS-сервер для статичного IP (типово — шлюз)")

    m = p.add_argument_group("режим роботи")
    m.add_argument("--mode", choices=[MODE_UPNP, MODE_PUSH], default=MODE_UPNP,
                   help="upnp — ESP32 сам відкриває порт на роутері (типово); push — ESP32 сам стукає на сервер")
    m.add_argument("--upnp-ports", metavar="ПОРТИ", default=DEFAULT_UPNP_PORTS,
                   help=f"зовнішні порти-кандидати через кому; береться перший вільний (типово {DEFAULT_UPNP_PORTS})")
    m.add_argument("--server", metavar="АДРЕСА", help="IP або домен VPS для режиму push")
    m.add_argument("--server-port", metavar="ПОРТ", type=int, default=DEFAULT_SERVER_PORT,
                   help=f"порт прийому стуків на VPS (типово {DEFAULT_SERVER_PORT})")
    m.add_argument("--push-interval", metavar="СЕК", type=int, default=DEFAULT_PUSH_INTERVAL,
                   help=f"інтервал стуку в секундах (типово {DEFAULT_PUSH_INTERVAL})")

    r = p.add_argument_group("резервний пінг Світлобота")
    r.add_argument("--svitlobot-token", metavar="КЛЮЧ",
                   help="ключ каналу Світлобота: ESP32 сама пінгуватиме Світлобот щохвилини (на випадок, "
                        "якщо VPS недоступний). Не вказано — залишається збережений; порожній рядок — вимкнути")


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
            "  svitlo-flasher flash --ssid МояМережа --mode push --server vps.example.com\n"
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
    p.add_argument("--firmware", metavar="ФАЙЛ", help="шлях до образу прошивки (типово вбудований, за типом чипа)")
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
