"""Источник сведений об обновлениях операционной системы.

Monik ставится на разные системы, и менеджер пакетов у них разный.
Поэтому подсистема знает только понятие «обновление, доступное но не
установленное», а способ его получить остаётся переходником: реализация
под конкретный менеджер пакетов живёт отдельно и подключается
конфигурацией, а не условиями внутри логики.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from monik.domain.models.base import DomainModel

__all__ = ["PendingUpdate", "PendingUpdatesSource"]


class PendingUpdate(DomainModel):
    """Обновление, доступное системе, но не установленное."""

    #: Имя пакета.
    name: str
    #: Установленная версия, если её удалось определить.
    current_version: str | None = None
    #: Доступная версия.
    available_version: str | None = None
    #: Является ли обновление обновлением безопасности.
    security: bool = False

    def describe(self) -> str:
        """Строка для сообщения оператору."""
        if self.current_version and self.available_version:
            return f"{self.name}: {self.current_version} → {self.available_version}"
        if self.available_version:
            return f"{self.name}: {self.available_version}"
        return self.name


@runtime_checkable
class PendingUpdatesSource(Protocol):
    """Откуда берётся список доступных обновлений."""

    #: Команда, которой оператор применяет обновления вручную. Показывается
    #: в уведомлении, поэтому принадлежит источнику: у каждого менеджера
    #: пакетов она своя.
    apply_command: str

    async def pending(self) -> tuple[PendingUpdate, ...]:
        """Доступные, но не установленные обновления.

        Пустой набор означает «обновлений нет». Недоступность самого
        менеджера пакетов — ошибка, а не пустой набор: молчание не
        должно выглядеть как отсутствие обновлений.
        """
        ...
