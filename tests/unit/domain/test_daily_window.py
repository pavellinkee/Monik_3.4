"""Суточное окно работы."""

from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from monik.domain.value_objects.schedule import DailyWindow

LISBON = "Europe/Lisbon"


def _window(start: str, end: str, timezone: str = LISBON) -> DailyWindow:
    return DailyWindow(
        start=time.fromisoformat(start), end=time.fromisoformat(end), timezone=timezone
    )


def _at(hour: int, minute: int = 0, timezone: str = LISBON) -> datetime:
    return datetime(2026, 9, 13, hour, minute, tzinfo=ZoneInfo(timezone))


class TestDaytimeWindow:
    def test_moment_inside_the_window(self) -> None:
        assert _window("07:00", "19:00").contains(_at(12))

    def test_start_belongs_to_the_window(self) -> None:
        assert _window("07:00", "19:00").contains(_at(7))

    def test_end_does_not_belong_to_the_window(self) -> None:
        """Границы ``[начало, конец)``: соседние окна не перекрываются."""
        assert not _window("07:00", "19:00").contains(_at(19))

    def test_moment_before_and_after(self) -> None:
        window = _window("07:00", "19:00")
        assert not window.contains(_at(6, 59))
        assert not window.contains(_at(23))


class TestOvernightWindow:
    """Окно через полночь — не пустой промежуток."""

    def test_evening_belongs(self) -> None:
        assert _window("22:00", "04:00").contains(_at(23))

    def test_night_belongs(self) -> None:
        assert _window("22:00", "04:00").contains(_at(2))

    def test_daytime_does_not_belong(self) -> None:
        assert not _window("22:00", "04:00").contains(_at(12))

    def test_window_knows_it_crosses_midnight(self) -> None:
        assert _window("22:00", "04:00").crosses_midnight
        assert not _window("07:00", "19:00").crosses_midnight


class TestTimezone:
    def test_moment_is_converted_to_the_window_timezone(self) -> None:
        """Сервер может жить в другом поясе, окно от этого не сдвигается."""
        window = _window("07:00", "19:00", timezone="Europe/Lisbon")
        # 06:30 UTC — это 07:30 в Лиссабоне летом: окно уже открыто.
        assert window.contains(datetime(2026, 9, 13, 6, 30, tzinfo=UTC))
        # 19:30 UTC — 20:30 по Лиссабону: окно уже закрыто.
        assert not window.contains(datetime(2026, 9, 13, 19, 30, tzinfo=UTC))

    def test_unknown_timezone_is_rejected_at_creation(self) -> None:
        with pytest.raises(Exception, match="Mars"):
            _window("07:00", "19:00", timezone="Mars/Olympus")


def test_equal_bounds_are_rejected() -> None:
    """``07:00 → 07:00`` неоднозначно: это ноль часов или все сутки."""
    with pytest.raises(ValueError, match="must differ"):
        _window("07:00", "07:00")


def test_description_is_readable() -> None:
    assert _window("07:00", "19:00").describe() == "07:00–19:00 Europe/Lisbon"
