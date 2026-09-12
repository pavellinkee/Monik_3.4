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

__all__ = [
    "PendingUpdate",
    "PendingUpdatesSource",
    "SystemUpdateResult",
    "SystemUpdater",
]


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
    #: Раздел менеджера пакетов: по нему определяется категория.
    section: str | None = None

    def describe(self) -> str:
        """Строка для сообщения оператору.

        Пояснение на русском добавляет :mod:`monik.services.updates.explain`:
        имя пакета само по себе оператору мало что сообщает.
        """
        from monik.services.updates.explain import describe as explain

        if self.current_version and self.available_version:
            versions = f"{self.current_version} → {self.available_version}"
        elif self.available_version:
            versions = self.available_version
        else:
            versions = ""
        head = f"{self.name}: {versions}" if versions else self.name
        return f"{head}\n   {explain(self)}"


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


class SystemUpdateResult(DomainModel):
    """Чем закончилась попытка применить обновления."""

    #: Удалось ли применить обновления.
    applied: bool
    #: Короткое пояснение для оператора на русском языке.
    detail: str
    #: Сколько пакетов обновлено, если это удалось определить.
    packages: int | None = None


@runtime_checkable
class SystemUpdater(Protocol):
    """Применение ожидающих обновлений по команде оператора.

    Отделён от :class:`PendingUpdatesSource` намеренно: читать список
    обновлений может кто угодно, а устанавливать их — только по явному
    подтверждению и с повышенными правами. Разные права и разные
    последствия означают разные порты.

    Реализация обязана быть неинтерактивной: подтверждение оператор уже
    дал, а ответить на вопрос в консоли из Telegram невозможно.
    """

    async def available(self) -> bool:
        """Разрешена ли установка обновлений в этой системе.

        Проверяется до того, как оператору предложат установку: кнопка,
        которая заведомо ответит отказом, хуже её отсутствия. Право
        выдаётся системой, а не конфигурацией Monik, и может появиться
        или исчезнуть без перезапуска приложения — поэтому это вопрос, а
        не однажды прочитанная настройка.
        """
        ...

    async def apply(self) -> SystemUpdateResult:
        """Установить доступные обновления.

        Исключение не выпускается: невозможность установить — такой же
        ответ оператору, как и успех, и сообщается тем же путём.
        """
        ...
