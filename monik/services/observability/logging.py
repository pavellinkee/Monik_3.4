"""Structured logging.

Логи должны позволять определить cycle ID, opportunity ID, Level 2 ID,
request ID, агрегатор, ресурс, длительность и категорию ошибки
(``CLAUDE.md`` §48). Секреты в логи не попадают ни при каком уровне
логирования: редакция применяется к финальной записи, а не к аргументам
вызывающего кода.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from monik.domain.errors.base import MonikError
from monik.services.observability.context import current_context
from monik.services.observability.redaction import (
    SecretRegistry,
    redact_mapping,
    redact_text,
)

__all__ = ["StructuredFormatter", "configure_logging", "get_logger", "log_fields"]

#: Ключ, под которым структурированные поля передаются в ``logging``.
_FIELDS_KEY = "monik_fields"

#: Стандартные атрибуты ``LogRecord``; в структурированный вывод не попадают.
_RESERVED_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
        _FIELDS_KEY,
    }
)


def log_fields(**fields: Any) -> dict[str, Any]:
    """Собрать структурированные поля для передачи в ``logger.info(..., extra=...)``."""
    return {_FIELDS_KEY: fields}


def _resolve_timezone(name: str | None) -> tzinfo | None:
    """Пояс по имени IANA; при неизвестном имени — UTC.

    Неизвестный пояс не должен мешать запуску: логи важнее косметики
    времени, поэтому вместо отказа применяется UTC.
    """
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


class StructuredFormatter(logging.Formatter):
    """Форматирует запись как одну строку JSON.

    Редакция применяется к уже сериализованной строке, поэтому секрет
    не может утечь ни через сообщение, ни через структурированное поле,
    ни через текст исключения.
    """

    def __init__(
        self,
        *,
        registry: SecretRegistry | None = None,
        timezone: tzinfo | None = None,
    ) -> None:
        super().__init__()
        self._registry = registry
        self._timezone = timezone

    def formatTime(  # noqa: N802 - имя задано logging.Formatter
        self, record: logging.LogRecord, datefmt: str | None = None
    ) -> str:
        """Отметка времени записи в настроенном часовом поясе.

        Смещение остаётся в строке всегда, поэтому запись однозначна и
        при переходе на зимнее время: требование
        ``28_OBSERVABILITY.md`` §7 о timezone-aware отметках сохранено.
        Сам пояс задаётся конфигурацией — оператор читает логи в том же
        времени, в котором работает.
        """
        moment = datetime.fromtimestamp(record.created, tz=UTC)
        if self._timezone is not None:
            moment = moment.astimezone(self._timezone)
        return moment.strftime(datefmt or "%Y-%m-%dT%H:%M:%S%z")

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(current_context().as_fields())

        fields = getattr(record, _FIELDS_KEY, None)
        if isinstance(fields, Mapping):
            payload.update(fields)
        for name, value in record.__dict__.items():
            if name not in _RESERVED_ATTRS and not name.startswith("_"):
                payload.setdefault(name, value)

        if record.exc_info and record.exc_info[1] is not None:
            payload.update(_exception_fields(record.exc_info[1]))

        redacted = redact_mapping(payload, registry=self._registry)
        serialized = json.dumps(redacted, ensure_ascii=False, default=str, sort_keys=True)
        return redact_text(serialized, registry=self._registry)


def _exception_fields(exception: BaseException) -> dict[str, Any]:
    """Поля исключения для structured log.

    Traceback не включается: он может содержать значения переменных,
    включая секреты. Для диагностики достаточно типа, кода и категории.
    """
    fields: dict[str, Any] = {
        "error_type": type(exception).__name__,
        "error_message": str(exception),
    }
    if isinstance(exception, MonikError):
        fields.update(
            {
                "error_code": exception.info.code,
                "error_category": exception.info.category.value,
                "error_severity": exception.info.severity.value,
                "error_retryability": exception.info.retryability.value,
            }
        )
    return fields


def configure_logging(
    *,
    level: str = "INFO",
    stream: Any = None,
    registry: SecretRegistry | None = None,
    timezone: str | None = None,
) -> None:
    """Настроить корневой логгер приложения.

    Настройка централизована (``25_PROJECT_STRUCTURE.md`` §76): подсистемы
    получают логгер через :func:`get_logger` и не конфигурируют вывод сами.

    ``timezone`` — IANA-пояс отметок времени. Без него отметки остаются в
    UTC. Смещение печатается в любом случае, поэтому запись однозначна.
    """
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(
        StructuredFormatter(registry=registry, timezone=_resolve_timezone(timezone))
    )

    root = logging.getLogger("monik")
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    """Получить логгер подсистемы.

    Имя дополняется префиксом ``monik.``, чтобы вся конфигурация вывода
    оставалась в одном месте.
    """
    if name.startswith("monik"):
        return logging.getLogger(name)
    return logging.getLogger(f"monik.{name}")
