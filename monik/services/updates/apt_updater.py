"""Установка обновлений менеджером пакетов apt.

Отделён от чтения списка (:mod:`monik.services.updates.apt`) намеренно:
чтение безопасно и прав не требует, установка изменяет систему и требует
повышенных прав. Разные последствия — разные модули, и никакой код,
которому нужен только список, не получает доступ к установке.

Команда выполняется через ``sudo -n``: пароль не запрашивается, и если
право не выдано, попытка сразу заканчивается понятным ответом вместо
зависания. Список разрешённых команд задаётся системой, а не Monik.

Установка неинтерактивна: оператор подтвердил действие в Telegram и
ответить на вопрос в консоли уже не сможет. Поэтому конфигурационные
файлы при конфликте остаются прежними, а перезапуск затронутых служб
не поручается системе — Monik перезапускает себя сам, после того как
ответ оператору отправлен.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence

from monik.services.observability.logging import get_logger, log_fields
from monik.services.updates.ports import SystemUpdateResult

__all__ = ["AptSystemUpdater"]

_LOGGER = get_logger("services.updates")

#: Обновление списка пакетов перед установкой: устанавливать по устаревшему
#: кешу означало бы ставить не то, что было показано оператору.
_REFRESH_COMMAND: tuple[str, ...] = ("sudo", "-n", "apt-get", "update")

#: Установка обновлений. ``--force-confold`` оставляет существующие файлы
#: настроек: молча заменить чужую конфигурацию хуже, чем не обновить её.
_UPGRADE_COMMAND: tuple[str, ...] = (
    "sudo",
    "-n",
    "apt-get",
    "--yes",
    "--option",
    "Dpkg::Options::=--force-confold",
    "--option",
    "Dpkg::Options::=--force-confdef",
    "upgrade",
)

#: Окружение неинтерактивной установки. Задаётся целиком, а не
#: дополняет окружение процесса: привилегированная команда не должна
#: зависеть от того, с каким окружением запущена служба. ``PATH``
#: перечислен явно по той же причине.
#:
#: ``NEEDRESTART_MODE=l`` запрещает системе перезапускать службы
#: самостоятельно: перезапуск Monik должен произойти после ответа
#: оператору, иначе ответ не будет отправлен.
_ENVIRONMENT: dict[str, str] = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "DEBIAN_FRONTEND": "noninteractive",
    "NEEDRESTART_MODE": "l",
    "NEEDRESTART_SUSPEND": "1",
    "LC_ALL": "C",
}

#: Проверка права: выводит правило sudo для команды, ничего не выполняя.
_PERMISSION_COMMAND: tuple[str, ...] = ("sudo", "-n", "--list", "apt-get")

#: Итоговая строка apt: ``N upgraded, M newly installed, ...``.
_SUMMARY = re.compile(r"^(?P<upgraded>\d+)\s+upgraded", re.MULTILINE)

#: Предел ожидания: загрузка и установка пакетов заметно дольше чтения.
_TIMEOUT_SECONDS = 900.0

#: Признак того, что право на выполнение команды не выдано.
_SUDO_DENIED = ("a password is required", "may not run", "no tty present")


class AptSystemUpdater:
    """Установка обновлений через apt по подтверждённой команде."""

    def __init__(
        self,
        *,
        permission_command: Sequence[str] = _PERMISSION_COMMAND,
        refresh_command: Sequence[str] = _REFRESH_COMMAND,
        upgrade_command: Sequence[str] = _UPGRADE_COMMAND,
        timeout: float = _TIMEOUT_SECONDS,
    ) -> None:
        self._permission_command = tuple(permission_command)
        self._refresh_command = tuple(refresh_command)
        self._upgrade_command = tuple(upgrade_command)
        self._timeout = timeout

    async def available(self) -> bool:
        """Разрешена ли установка без пароля.

        Проверка ничего не устанавливает: ``sudo --list`` лишь сообщает,
        выдано ли право.
        """
        allowed, _ = await self._run(self._permission_command)
        return allowed

    async def apply(self) -> SystemUpdateResult:
        """Установить доступные обновления.

        Неудачное обновление списка пакетов не прерывает установку: кеш
        уже есть, и поставить по нему лучше, чем не поставить ничего.
        Неудача самой установки возвращается как результат, а не как
        исключение: оператор ждёт ответа в том же диалоге.
        """
        refreshed, refresh_output = await self._run(self._refresh_command)
        if not refreshed:
            _LOGGER.warning(
                "package list refresh failed before upgrade",
                extra=log_fields(detail=_reason(refresh_output)),
            )
        succeeded, output = await self._run(self._upgrade_command)
        if not succeeded:
            reason = _reason(output)
            _LOGGER.warning("system upgrade failed", extra=log_fields(detail=reason))
            return SystemUpdateResult(applied=False, detail=reason)
        packages = _upgraded_count(output)
        _LOGGER.warning("system upgrade applied", extra=log_fields(packages=packages))
        return SystemUpdateResult(
            applied=True,
            detail="обновления установлены",
            packages=packages,
        )

    async def _run(self, command: tuple[str, ...]) -> tuple[bool, str]:
        """Выполнить команду; вернуть признак успеха и её вывод."""
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=_ENVIRONMENT,
            )
        except OSError as error:
            return False, f"команда недоступна: {error.strerror or error}"
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=self._timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            return False, "менеджер пакетов не ответил вовремя"
        return process.returncode == 0, stdout.decode("utf-8", errors="replace")


def _reason(output: str) -> str:
    """Причина отказа на русском языке.

    Отдельно распознаётся отсутствие права на выполнение: это не поломка
    обновлений, а незавершённая настройка сервера, и оператору нужно
    знать разницу.
    """
    lowered = output.lower()
    if any(marker in lowered for marker in _SUDO_DENIED):
        return "нет права выполнять установку без пароля"
    tail = [line.strip() for line in output.strip().splitlines() if line.strip()]
    if not tail:
        return "менеджер пакетов завершился с ошибкой"
    return tail[-1][:200]


def _upgraded_count(output: str) -> int | None:
    """Сколько пакетов обновлено по итоговой строке apt."""
    match = _SUMMARY.search(output)
    return int(match.group("upgraded")) if match is not None else None
