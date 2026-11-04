"""Робота з ESP32 через послідовний порт: протокол налаштування SVB:*."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import serial
from serial.tools import list_ports

BAUD_RATE = 115200

MODE_UPNP = "upnp"
MODE_PUSH = "push"
DEFAULT_UPNP_PORTS = "18080,18081,18082,18083,18084"
DEFAULT_SERVER_PORT = 8081
DEFAULT_PUSH_INTERVAL = 20

# Відомі USB-UART мости, які зазвичай стоять на платах ESP32.
KNOWN_USB_IDS = {
    (0x10C4, 0xEA60): "CP210x",
    (0x1A86, 0x7523): "CH340",
    (0x1A86, 0x55D4): "CH9102",
    (0x0403, 0x6001): "FTDI",
    (0x0403, 0x6010): "FTDI",
    (0x303A, 0x1001): "ESP32 USB",
}


class DeviceError(Exception):
    """Помилка взаємодії з пристроєм."""


@dataclass
class PortInfo:
    device: str
    description: str
    likely_esp: bool


@dataclass
class DeviceConfig:
    ssid: str
    password: str = ""
    port: int = 8080
    key: str = ""
    hostname: str = "svitlobot"
    ip: str = ""
    gateway: str = ""
    subnet: str = ""
    dns: str = ""
    mode: str = MODE_UPNP
    upnp_ports: str = DEFAULT_UPNP_PORTS
    server: str = ""
    server_port: int = DEFAULT_SERVER_PORT
    push_interval: int = DEFAULT_PUSH_INTERVAL

    def to_json(self) -> str:
        data = {
            "ssid": self.ssid,
            "password": self.password,
            "port": self.port,
            "key": self.key,
            "hostname": self.hostname,
            "ip": self.ip,
            "gateway": self.gateway,
            "subnet": self.subnet,
            "dns": self.dns,
            "mode": self.mode,
            "upnp_ports": self.upnp_ports,
            "server": self.server,
            "server_port": self.server_port,
            "push_interval": self.push_interval,
        }
        # ensure_ascii=True: кирилиця в SSID передається як \uXXXX — це безпечно для UART.
        return json.dumps(data, ensure_ascii=True, separators=(",", ":"))


def list_serial_ports() -> list[PortInfo]:
    """Повертає список послідовних портів, позначаючи ймовірні ESP32."""
    result = []
    for p in list_ports.comports():
        likely = (p.vid, p.pid) in KNOWN_USB_IDS
        chip = KNOWN_USB_IDS.get((p.vid, p.pid))
        description = p.description if p.description and p.description != "n/a" else "—"
        if chip and chip not in description:
            description = f"{description} [{chip}]".strip()
        result.append(PortInfo(p.device, description, likely))
    # Ймовірні ESP32 — першими.
    result.sort(key=lambda x: (not x.likely_esp, x.device))
    return result


@dataclass
class Session:
    """Відкрите з'єднання з пристроєм."""

    port: str
    on_log: Optional[Callable[[str], None]] = None
    _ser: serial.Serial = field(init=False, repr=False)
    _buf: bytes = field(default=b"", init=False, repr=False)

    def __post_init__(self) -> None:
        ser = serial.Serial()
        ser.port = self.port
        ser.baudrate = BAUD_RATE
        ser.timeout = 0.1
        # Не смикаємо DTR/RTS під час відкриття, щоб випадково не ввести чип у режим завантажувача.
        ser.dtr = False
        ser.rts = False
        try:
            ser.open()
        except serial.SerialException as exc:
            raise DeviceError(f"не вдалося відкрити порт {self.port}: {exc}") from exc
        self._ser = ser

    def close(self) -> None:
        try:
            self._ser.close()
        except Exception:
            pass

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def reset(self) -> None:
        """Апаратне перезавантаження через лінію RTS (EN), IO0 лишається у високому рівні."""
        self._ser.dtr = False
        self._ser.rts = True
        time.sleep(0.15)
        self._ser.rts = False
        self._buf = b""
        self._ser.reset_input_buffer()

    def send(self, line: str) -> None:
        self._ser.write((line + "\n").encode("utf-8"))
        self._ser.flush()

    def read_line(self, timeout: float) -> Optional[str]:
        """Читає один рядок; None, якщо час вичерпано."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if b"\n" in self._buf:
                raw, self._buf = self._buf.split(b"\n", 1)
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    return line
                continue
            chunk = self._ser.read(256)
            if chunk:
                self._buf += chunk
        return None

    def wait_for(self, prefixes: tuple[str, ...], timeout: float) -> Optional[str]:
        """Чекає рядок, що починається з одного з префіксів. Інші рядки передає в on_log."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            line = self.read_line(remaining)
            if line is None:
                return None
            if line.startswith(prefixes):
                return line
            if self.on_log:
                self.on_log(line)

    def handshake(self, timeout: float = 10.0) -> None:
        """Переконується, що на порту наша прошивка і вона відповідає."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.send("SVB:PING")
            if self.wait_for(("SVB:PONG",), 1.0):
                return
        raise DeviceError(
            "пристрій не відповідає. Переконайтеся, що на ESP32 записана прошивка SvitloBot "
            "(команда flash) і що обрано правильний порт."
        )

    def get_info(self) -> dict:
        self.send("SVB:GET")
        line = self.wait_for(("SVB:INFO ",), 3.0)
        if not line:
            raise DeviceError("пристрій не повернув інформацію")
        try:
            return json.loads(line[len("SVB:INFO "):])
        except json.JSONDecodeError as exc:
            raise DeviceError(f"некоректна відповідь пристрою: {exc}") from exc

    def apply_config(self, config: DeviceConfig) -> None:
        self.send("SVB:SET " + config.to_json())
        line = self.wait_for(("SVB:OK", "SVB:ERR"), 5.0)
        if line is None:
            raise DeviceError("пристрій не підтвердив збереження налаштувань")
        if line.startswith("SVB:ERR"):
            raise DeviceError("пристрій відхилив налаштування: " + line[len("SVB:ERR"):].strip())

    def wait_for_ip(self, timeout: float = 45.0) -> Optional[str]:
        """Чекає, поки пристрій підключиться до Wi-Fi. Повертає IP або None."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            line = self.wait_for(("SVB:IP ", "SVB:WIFI_TIMEOUT", "SVB:CONNECTING"), remaining)
            if line is None:
                return None
            if line.startswith("SVB:IP "):
                return line[len("SVB:IP "):].strip()
            if line.startswith("SVB:WIFI_TIMEOUT"):
                return None
            if self.on_log:
                self.on_log(line)

    def wait_for_mode_result(self, mode: str, timeout: float = 40.0) -> tuple[bool, str, list[str]]:
        """Чекає перший результат UPnP або стуку. Повертає (успіх, текст, додаткові рядки)."""
        prefix = "SVB:UPNP " if mode == MODE_UPNP else "SVB:PUSH "
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, "пристрій не повідомив результат за відведений час", []
            line = self.wait_for((prefix,), remaining)
            if line is None:
                return False, "пристрій не повідомив результат за відведений час", []
            rest = line[len(prefix):]
            # Очікування NTP — проміжний стан, чекаємо далі.
            if mode == MODE_PUSH and "NTP" in rest:
                if self.on_log:
                    self.on_log(line)
                continue
            extra = []
            if rest.startswith("OK") and mode == MODE_UPNP:
                cgnat = self.wait_for(("SVB:UPNP_CGNAT",), 1.5)
                if cgnat:
                    extra.append(cgnat[len("SVB:UPNP_CGNAT"):].strip())
            ok = rest.startswith("OK")
            return ok, rest[2:].strip() if ok else rest[4:].strip(), extra

    def clear_config(self) -> None:
        self.send("SVB:CLEAR")
        if not self.wait_for(("SVB:OK",), 5.0):
            raise DeviceError("пристрій не підтвердив скидання налаштувань")

    def raw_read(self, timeout: float) -> Optional[str]:
        return self.read_line(timeout)
