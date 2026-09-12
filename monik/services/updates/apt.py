"""Переходник к менеджеру пакетов apt.

Единственное место, где Monik знает про apt. Логика подсистемы работает
с понятием :class:`PendingUpdate` и о менеджере пакетов не знает ничего:
другая система подключается второй реализацией
:class:`PendingUpdatesSource`, а не правкой общей логики.

Команда выполняется только на чтение и не требует прав root: список
доступных обновлений берётся из локального кеша apt. Ничего не
устанавливается и не изменяется — решение о применении обновлений
остаётся за оператором.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence

from monik.domain.errors import ProviderError
from monik.services.updates.ports import PendingUpdate

__all__ = ["AptPendingUpdates"]

#: Команда получения списка. Только чтение локального кеша.
_COMMAND: tuple[str, ...] = ("apt", "list", "--upgradable")

#: Строка вывода: ``пакет/источник версия архитектура [upgradable from: версия]``.
_LINE = re.compile(
    r"^(?P<name>[^/\s]+)/(?P<origin>\S+)\s+(?P<available>\S+)\s+\S+"
    r"(?:\s+\[upgradable from:\s*(?P<current>[^\]]+)\])?\s*$"
)

#: Признак источника обновлений безопасности в имени репозитория.
_SECURITY = "-security"

#: Предел ожидания: менеджер пакетов может ждать снятия блокировки.
_TIMEOUT_SECONDS = 60.0


class AptPendingUpdates:
    """Список доступных обновлений по данным apt."""

    apply_command = "sudo apt update && sudo apt upgrade"

    def __init__(self, *, command: Sequence[str] = _COMMAND, timeout: float = _TIMEOUT_SECONDS):
        self._command = tuple(command)
        self._timeout = timeout

    async def pending(self) -> tuple[PendingUpdate, ...]:
        """Доступные, но не установленные обновления."""
        output = await self._run()
        return tuple(update for line in output.splitlines() if (update := _parse(line)) is not None)

    async def _run(self) -> str:
        process = await asyncio.create_subprocess_exec(
            *self._command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=self._timeout)
        except TimeoutError as error:
            process.kill()
            await process.wait()
            raise ProviderError(
                "package manager did not answer in time",
                code="updates_check_timeout",
            ) from error
        if process.returncode != 0:
            # Недоступность менеджера пакетов — ошибка, а не «обновлений
            # нет»: молчание не должно выглядеть как отсутствие обновлений.
            raise ProviderError(
                f"package manager exited with code {process.returncode}",
                code="updates_check_failed",
            )
        return stdout.decode("utf-8", errors="replace")


def _parse(line: str) -> PendingUpdate | None:
    """Разобрать одну строку вывода; посторонние строки пропускаются."""
    match = _LINE.match(line.strip())
    if match is None:
        return None
    origin = match.group("origin")
    return PendingUpdate(
        name=match.group("name"),
        current_version=match.group("current"),
        available_version=match.group("available"),
        security=_SECURITY in origin,
    )
