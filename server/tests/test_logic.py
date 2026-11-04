"""Тести чистої логіки визначення стану."""

from app.logic import (
    Debouncer,
    PowerState,
    UnknownPolicy,
    classify,
    format_duration,
    should_ping_svitlobot,
)

ON, OFF, UNK = PowerState.ON, PowerState.OFF, PowerState.UNKNOWN


def test_classify_matrix():
    assert classify(True, True) == ON
    assert classify(True, False) == ON  # роутер не відповідає на пінг, але ESP32 доступний
    assert classify(False, True) == OFF
    assert classify(False, False) == UNK


def test_debouncer_requires_consecutive_results():
    d = Debouncer()
    assert d.feed(ON, 3) is None
    assert d.feed(ON, 3) is None
    t = d.feed(ON, 3)
    assert t is not None and t.previous is None and t.current == ON

    # Поодинокий збій не змінює стан
    assert d.feed(OFF, 3) is None
    assert d.feed(ON, 3) is None
    assert d.feed(OFF, 3) is None
    assert d.feed(OFF, 3) is None
    t = d.feed(OFF, 3)
    assert t is not None and t.previous == ON and t.current == OFF


def test_debouncer_candidate_switch_resets_counter():
    d = Debouncer(confirmed=ON)
    d.feed(OFF, 2)
    d.feed(UNK, 2)  # інший кандидат — лічильник з початку
    assert d.confirmed == ON
    t = d.feed(UNK, 2)
    assert t is not None and t.current == UNK


def test_debouncer_threshold_one():
    d = Debouncer(confirmed=ON)
    t = d.feed(OFF, 1)
    assert t is not None and t.current == OFF


def test_should_ping():
    assert should_ping_svitlobot(ON, ON, UnknownPolicy.KEEP_LAST)
    assert not should_ping_svitlobot(OFF, OFF, UnknownPolicy.ASSUME_ON)

    assert should_ping_svitlobot(UNK, ON, UnknownPolicy.KEEP_LAST)
    assert not should_ping_svitlobot(UNK, OFF, UnknownPolicy.KEEP_LAST)
    assert not should_ping_svitlobot(UNK, None, UnknownPolicy.KEEP_LAST)
    assert should_ping_svitlobot(UNK, OFF, UnknownPolicy.ASSUME_ON)
    assert not should_ping_svitlobot(UNK, ON, UnknownPolicy.STOP)

    # Після запуску, поки стан не визначено
    assert should_ping_svitlobot(None, ON, UnknownPolicy.STOP)
    assert not should_ping_svitlobot(None, None, UnknownPolicy.ASSUME_ON)


def test_format_duration():
    assert format_duration(45) == "45с"
    assert format_duration(125) == "2хв"
    assert format_duration(3 * 3600 + 5 * 60) == "3год 5хв"
    assert format_duration(86400 + 3600) == "1д 1год"
