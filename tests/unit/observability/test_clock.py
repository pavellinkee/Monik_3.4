"""Тесты абстракции времени."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from monik.services.observability import Clock, FakeClock, SystemClock
from monik.services.observability.logging import StructuredFormatter, configure_logging

START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def test_system_clock_satisfies_protocol() -> None:
    assert isinstance(SystemClock(), Clock)


def test_fake_clock_satisfies_protocol() -> None:
    assert isinstance(FakeClock(START), Clock)


def test_system_clock_returns_utc_aware_time() -> None:
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_fake_clock_does_not_move_on_its_own() -> None:
    """Тесты должны быть детерминированными (23 §17-18)."""
    clock = FakeClock(START)
    assert clock.now() == START
    assert clock.now() == START


def test_fake_clock_advances_explicitly() -> None:
    clock = FakeClock(START)
    clock.advance(timedelta(minutes=5))
    assert clock.now() == START + timedelta(minutes=5)
    assert clock.monotonic() == 300.0


def test_fake_clock_set_to() -> None:
    clock = FakeClock(START)
    clock.set_to(START + timedelta(hours=1))
    assert clock.now() == START + timedelta(hours=1)
    assert clock.monotonic() == 3600.0


def test_fake_clock_rejects_backwards_movement() -> None:
    clock = FakeClock(START)
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(timedelta(seconds=-1))
    with pytest.raises(ValueError, match="backwards"):
        clock.set_to(START - timedelta(seconds=1))


def test_fake_clock_requires_aware_start() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FakeClock(datetime(2026, 1, 1, 12, 0))


def test_fake_clock_normalizes_to_utc() -> None:
    clock = FakeClock(datetime.fromisoformat("2026-01-01T13:00:00+01:00"))
    assert clock.now() == START


class TestLogTimezone:
    """Отметки времени в логах идут в настроенном поясе.

    Оператор читает логи в том же времени, в котором работает. Смещение
    остаётся в строке всегда, поэтому запись однозначна и при переходе на
    зимнее время (``28_OBSERVABILITY.md`` §7 о timezone-aware отметках).
    """

    @staticmethod
    def _record(created: float) -> logging.LogRecord:
        record = logging.LogRecord(
            name="monik.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="проверка",
            args=(),
            exc_info=None,
        )
        record.created = created
        return record

    #: Полдень UTC 1 июля: Лиссабон в летнем времени, смещение +01:00.
    SUMMER = datetime(2026, 7, 1, 12, 0, tzinfo=UTC).timestamp()
    #: Полдень UTC 1 января: Лиссабон в зимнем времени, смещение +00:00.
    WINTER = datetime(2026, 1, 1, 12, 0, tzinfo=UTC).timestamp()

    def test_configured_timezone_is_applied(self) -> None:
        formatter = StructuredFormatter(timezone=ZoneInfo("Europe/Lisbon"))
        stamp = formatter.formatTime(self._record(self.SUMMER), "%Y-%m-%dT%H:%M:%S%z")
        assert stamp == "2026-07-01T13:00:00+0100"

    def test_offset_is_always_present(self) -> None:
        """Без смещения запись была бы неоднозначной."""
        formatter = StructuredFormatter(timezone=ZoneInfo("Europe/Lisbon"))
        summer = formatter.formatTime(self._record(self.SUMMER), "%Y-%m-%dT%H:%M:%S%z")
        winter = formatter.formatTime(self._record(self.WINTER), "%Y-%m-%dT%H:%M:%S%z")

        assert summer.endswith("+0100")
        assert winter.endswith("+0000")

    def test_without_timezone_records_stay_in_utc(self) -> None:
        formatter = StructuredFormatter()
        stamp = formatter.formatTime(self._record(self.SUMMER), "%Y-%m-%dT%H:%M:%S%z")
        assert stamp == "2026-07-01T12:00:00+0000"

    def test_unknown_timezone_does_not_break_logging(self) -> None:
        """Логи важнее косметики времени: вместо отказа применяется UTC."""
        configure_logging(level="INFO", timezone="Nowhere/Unknown")
        logger = logging.getLogger("monik.test")
        assert logger.isEnabledFor(logging.INFO)
