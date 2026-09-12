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

from monik.domain.errors import MonikError, ProviderError
from monik.services.updates.ports import PendingUpdate

__all__ = ["AptPendingUpdates"]

#: Команда получения списка. Только чтение локального кеша.
_COMMAND: tuple[str, ...] = ("apt", "list", "--upgradable")

#: Строка вывода: ``пакет/источник версия архитектура [upgradable from: версия]``.
_LINE = re.compile(
    r"^(?P<name>[^/\s]+)/(?P<origin>\S+)\s+(?P<available>\S+)\s+\S+"
    r"(?:\s+\[upgradable from:\s*(?P<current>[^\]]+)\])?\s*$"
)

#: Команда получения сведений о пакетах, включая раздел.
_SECTION_COMMAND: tuple[str, ...] = ("apt-cache", "show", "--no-all-versions")

#: Строка раздела в выводе ``apt-cache show``.
_SECTION_LINE = re.compile(r"^Section:\s*(?P<section>\S+)\s*$")

#: Строка имени пакета в выводе ``apt-cache show``.
_PACKAGE_LINE = re.compile(r"^Package:\s*(?P<name>\S+)\s*$")


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
        output = await self._run(self._command)
        updates = tuple(
            update for line in output.splitlines() if (update := _parse(line)) is not None
        )
        return await self._with_sections(updates)

    async def _with_sections(self, updates: tuple[PendingUpdate, ...]) -> tuple[PendingUpdate, ...]:
        """Дополнить раздел пакета: по нему строится русское пояснение.

        Раздел запрашивается одной командой на все пакеты, а не по
        одному. Неудача здесь не отменяет уведомление: без раздела
        пояснение станет общим, но список останется полезным.
        """
        if not updates:
            return updates
        try:
            output = await self._run((*_SECTION_COMMAND, *(u.name for u in updates)))
        except MonikError:
            return updates
        sections = _parse_sections(output)
        return tuple(
            update.model_copy(update={"section": sections.get(update.name)}) for update in updates
        )

    async def _run(self, command: tuple[str, ...]) -> str:
        process = await asyncio.create_subprocess_exec(
            *command,
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


def _parse_sections(output: str) -> dict[str, str]:
    """Разделы пакетов из вывода ``apt-cache show``."""
    sections: dict[str, str] = {}
    name: str | None = None
    for line in output.splitlines():
        package = _PACKAGE_LINE.match(line)
        if package is not None:
            name = package.group("name")
            continue
        section = _SECTION_LINE.match(line)
        if section is not None and name is not None:
            sections.setdefault(name, section.group("section"))
    return sections


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
