"""Напоминание о доступных обновлениях системы."""

from __future__ import annotations

import pytest

from monik.domain.errors import ProviderError
from monik.services.updates.apt import AptPendingUpdates, _parse
from monik.services.updates.ports import PendingUpdate
from monik.services.updates.service import UpdateWatcher

#: Реальный вывод apt, снятый с рабочего сервера.
REAL_OUTPUT = """Listing...
base-files/noble-updates 13ubuntu10.5 amd64 [upgradable from: 13ubuntu10.4]
python3-apt/noble-updates 2.7.7ubuntu5.3 amd64 [upgradable from: 2.7.7ubuntu5.2]
openssl/noble-security 3.0.13-0ubuntu3.16 amd64 [upgradable from: 3.0.13-0ubuntu3.15]
"""


class TestParsing:
    def test_real_apt_output_is_parsed(self) -> None:
        updates = [u for line in REAL_OUTPUT.splitlines() if (u := _parse(line)) is not None]

        assert len(updates) == 3
        assert updates[0].name == "base-files"
        assert updates[0].current_version == "13ubuntu10.4"
        assert updates[0].available_version == "13ubuntu10.5"

    def test_security_origin_is_recognised(self) -> None:
        updates = [u for line in REAL_OUTPUT.splitlines() if (u := _parse(line)) is not None]

        assert [u.security for u in updates] == [False, False, True]

    def test_service_lines_are_ignored(self) -> None:
        assert _parse("Listing...") is None
        assert _parse("") is None
        assert _parse("WARNING: apt does not have a stable CLI interface") is None

    def test_description_is_readable(self) -> None:
        update = PendingUpdate(name="curl", current_version="8.5.0", available_version="8.5.1")
        assert update.describe().startswith("curl: 8.5.0 → 8.5.1\n")


class _Source:
    """Источник с заданным ответом."""

    apply_command = "sudo apt upgrade"

    def __init__(self, updates: tuple[PendingUpdate, ...] | Exception) -> None:
        self._updates = updates

    async def pending(self) -> tuple[PendingUpdate, ...]:
        if isinstance(self._updates, Exception):
            raise self._updates
        return self._updates


class _Notifier:
    """Получатель сообщения, запоминающий вызовы."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str]] = []
        self.actions: list[str | None] = []

    async def notify_pending_updates(
        self,
        updates: tuple[str, ...],
        *,
        apply_command: str,
        apply_action: str | None = None,
    ) -> bool:
        self.calls.append((updates, apply_command))
        self.actions.append(apply_action)
        return True


class TestWatcher:
    async def test_pending_updates_are_reported(self) -> None:
        source = _Source((PendingUpdate(name="curl", available_version="8.5.1"),))
        notifier = _Notifier()

        assert await UpdateWatcher(source, notifier).check() == 1
        assert notifier.calls
        assert notifier.calls[0][1] == "sudo apt upgrade"

    async def test_nothing_pending_sends_nothing(self) -> None:
        notifier = _Notifier()

        assert await UpdateWatcher(_Source(()), notifier).check() == 0
        assert notifier.calls == []

    async def test_broken_package_manager_is_not_silence(self) -> None:
        """Сбой проверки не должен выглядеть как отсутствие обновлений."""
        notifier = _Notifier()
        source = _Source(ProviderError("apt unavailable", code="updates_check_failed"))

        assert await UpdateWatcher(source, notifier).check() == 0
        assert notifier.calls == []

    async def test_failure_does_not_propagate(self) -> None:
        """Диагностика не должна ронять планировщик."""
        source = _Source(ProviderError("apt unavailable", code="updates_check_failed"))
        await UpdateWatcher(source, None).check()

    async def test_missing_notifier_is_allowed(self) -> None:
        source = _Source((PendingUpdate(name="curl"),))
        assert await UpdateWatcher(source, None).check() == 1


class TestAptSource:
    async def test_failed_command_raises(self) -> None:
        source = AptPendingUpdates(command=("false",))
        with pytest.raises(ProviderError, match="package manager"):
            await source.pending()

    async def test_apply_action_reaches_notifier(self) -> None:
        """Кнопка установки должна дойти до сообщения без изменений."""
        source = _Source((PendingUpdate(name="curl", available_version="8.5.1"),))
        notifier = _Notifier()

        await UpdateWatcher(source, notifier, apply_action="do:update").check()

        assert notifier.actions == ["do:update"]

    async def test_output_is_parsed_end_to_end(self) -> None:
        source = AptPendingUpdates(command=("printf", REAL_OUTPUT))
        updates = await source.pending()
        assert [u.name for u in updates] == ["base-files", "python3-apt", "openssl"]
