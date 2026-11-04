"""Чиста логіка визначення стану. Без вводу-виводу, щоб легко тестувати."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class PowerState(str, Enum):
    ON = "on"            # світло є
    OFF = "off"          # світла немає
    UNKNOWN = "unknown"  # немає зв'язку з об'єктом (ймовірно, зник інтернет)


STATE_LABELS = {
    PowerState.ON: "світло є",
    PowerState.OFF: "світла немає",
    PowerState.UNKNOWN: "невідомо",
}


class UnknownPolicy(str, Enum):
    KEEP_LAST = "keep_last"   # пінгувати, якщо останній відомий стан — «світло є»
    STOP = "stop"             # не пінгувати (Світлобот покаже відключення)
    ASSUME_ON = "assume_on"   # завжди пінгувати (вважати, що світло є)


def classify(http_ok: bool, host_reachable: bool) -> PowerState:
    """Визначає миттєвий стан за результатами перевірок."""
    if http_ok:
        return PowerState.ON
    if host_reachable:
        return PowerState.OFF
    return PowerState.UNKNOWN


@dataclass
class Transition:
    previous: Optional[PowerState]
    current: PowerState


@dataclass
class Debouncer:
    """Підтверджує зміну стану лише після N однакових перевірок поспіль.

    Захищає від хибних перемикань через поодинокі втрачені пакети.
    """

    confirmed: Optional[PowerState] = None
    candidate: Optional[PowerState] = None
    count: int = 0

    def feed(self, observed: PowerState, threshold: int) -> Optional[Transition]:
        threshold = max(1, threshold)
        if observed == self.confirmed:
            self.candidate = None
            self.count = 0
            return None
        if observed == self.candidate:
            self.count += 1
        else:
            self.candidate = observed
            self.count = 1
        if self.count >= threshold:
            transition = Transition(self.confirmed, observed)
            self.confirmed = observed
            self.candidate = None
            self.count = 0
            return transition
        return None


def should_ping_svitlobot(
    confirmed: Optional[PowerState],
    last_known_power: Optional[PowerState],
    policy: UnknownPolicy,
) -> bool:
    """Чи треба зараз надсилати пінг у Світлобот."""
    if confirmed == PowerState.ON:
        return True
    if confirmed == PowerState.OFF:
        return False
    if confirmed is None:
        # Стан ще не визначено після запуску — спираємося на збережений.
        return last_known_power == PowerState.ON
    # confirmed == UNKNOWN
    if policy == UnknownPolicy.ASSUME_ON:
        return True
    if policy == UnknownPolicy.STOP:
        return False
    return last_known_power == PowerState.ON


def format_duration(seconds: float) -> str:
    """Тривалість у стилі Світлобота: «2год 5хв», «45с»."""
    seconds = int(max(0, seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}д")
    if hours:
        parts.append(f"{hours}год")
    if minutes:
        parts.append(f"{minutes}хв")
    if not parts:
        parts.append(f"{secs}с")
    return " ".join(parts)
