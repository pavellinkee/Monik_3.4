"""Напоминание о доступных обновлениях системы.

Автоматически ставятся только обновления безопасности, поэтому остальные
накапливаются и ждут решения оператора. Задача раз в сутки собирает их
список и присылает уведомление с командой применения.

Подсистема ничего не устанавливает и не изменяет: она только читает
список и сообщает. Решение остаётся за оператором — обновление требует
перезапуска сканера, а момент перезапуска выбирает он.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from monik.domain.errors import MonikError
from monik.services.observability.logging import get_logger, log_fields
from monik.services.updates.ports import PendingUpdatesSource

__all__ = ["PendingUpdatesNotifier", "UpdateWatcher"]


@runtime_checkable
class PendingUpdatesNotifier(Protocol):
    """Минимальный контракт получателя сообщения.

    Объявлен здесь, чтобы подсистема не зависела от всей системы
    уведомлений: ей нужен ровно один метод.
    """

    async def notify_pending_updates(self, updates: tuple[str, ...], *, apply_command: str) -> bool:
        """Сообщить оператору о доступных обновлениях."""
        ...


_LOGGER = get_logger("services.updates")


class UpdateWatcher:
    """Проверяет доступные обновления и сообщает о них оператору."""

    def __init__(
        self, source: PendingUpdatesSource, notifier: PendingUpdatesNotifier | None
    ) -> None:
        self._source = source
        self._notifier = notifier

    async def check(self) -> int:
        """Проверить обновления и сообщить о них. Возвращает их число.

        Сбой проверки не считается отсутствием обновлений и не роняет
        вызвавшего: это диагностика, а не бизнес-результат.
        """
        try:
            updates = await self._source.pending()
        except MonikError as error:
            _LOGGER.warning(
                "pending updates check failed",
                extra=log_fields(error_code=error.info.code, detail=error.info.message),
            )
            return 0

        if not updates:
            _LOGGER.info("no pending system updates")
            return 0

        security = sum(1 for update in updates if update.security)
        _LOGGER.info(
            "pending system updates",
            extra=log_fields(pending=len(updates), security=security),
        )
        if self._notifier is not None:
            await self._notifier.notify_pending_updates(
                tuple(update.describe() for update in updates),
                apply_command=self._source.apply_command,
            )
        return len(updates)
