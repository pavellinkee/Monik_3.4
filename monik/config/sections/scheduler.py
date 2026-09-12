"""Конфигурация Scheduler."""

from __future__ import annotations

from typing import Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, model_validator

from monik.config.base import ConfigSection
from monik.domain.enums.scheduler import OverlapPolicy, TaskMode

__all__ = ["SchedulerConfig", "TaskScheduleConfig"]


class TaskScheduleConfig(ConfigSection):
    """Расписание одной задачи (``17_CONFIGURATION.md`` §44).

    Поддерживаются режимы STARTUP, INTERVAL, DAILY, WEEKLY и MANUAL
    (``14_SCHEDULER.md`` §12-15). Для DAILY и WEEKLY обязательны время в
    формате ``HH:MM`` и валидная IANA timezone (``17_CONFIGURATION.md``
    §18-19); WEEKLY дополнительно требует день недели.
    """

    enabled: bool = True
    mode: TaskMode
    interval_seconds: int | None = Field(default=None, ge=1, le=86_400)
    interval_days: int | None = Field(default=None, ge=1, le=365)
    time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    #: День недели для WEEKLY по ISO: 1 — понедельник, 6 — суббота.
    weekday: int | None = Field(default=None, ge=1, le=7)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    overlap_policy: OverlapPolicy = OverlapPolicy.SKIP

    @model_validator(mode="after")
    def _validate(self) -> Self:
        # ``interval_seconds`` у INTERVAL-задачи может отсутствовать: для
        # задач, у которых период задаётся настройкой своей подсистемы,
        # значение подставляет конфигурация целиком. Так параметром
        # управляют в одном месте, а расписание на него ссылается.
        # Отсутствие значения и после подстановки — ошибка, и она
        # проверяется там, где известны обе стороны.
        if self.mode is not TaskMode.INTERVAL and self.interval_seconds is not None:
            raise ValueError(f"interval_seconds is not applicable to {self.mode.value} task")
        if self.mode in {TaskMode.DAILY, TaskMode.WEEKLY}:
            if self.time is None:
                raise ValueError(f"{self.mode.value.upper()} task requires time in HH:MM format")
            if self.timezone is None:
                raise ValueError(f"{self.mode.value.upper()} task requires an explicit timezone")
        elif self.time is not None:
            raise ValueError(f"time is not applicable to {self.mode.value} task")
        if self.mode is TaskMode.WEEKLY:
            if self.weekday is None:
                raise ValueError("WEEKLY task requires weekday (1 = Monday .. 7 = Sunday)")
        elif self.weekday is not None:
            raise ValueError(f"weekday is not applicable to {self.mode.value} task")
        if self.timezone is not None:
            try:
                ZoneInfo(self.timezone)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError(f"invalid IANA timezone: {self.timezone!r}") from exc
        return self


class SchedulerConfig(ConfigSection):
    """Набор запланированных задач.

    Scheduler координирует запуск, но не содержит business logic
    (``14_SCHEDULER.md`` §3).
    """

    enabled: bool = True
    tasks: dict[str, TaskScheduleConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        for task_id in self.tasks:
            if not task_id or len(task_id) > 64:
                raise ValueError(f"invalid scheduler task id: {task_id!r}")
        return self
