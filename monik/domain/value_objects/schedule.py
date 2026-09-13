"""Суточное окно работы.

Окно описывает, в какие часы ресурс разрешено использовать. Понятие
универсальное: им пользуется провайдер котировок, но ни одна его
особенность в самом окне не закодирована.

Окно может пересекать полночь. ``22:00 → 04:00`` означает вечер и ночь,
а не пустой промежуток: иначе расписание было бы непригодно для
ресурсов, у которых удобное время приходится на ночные часы.

Границы трактуются как ``[начало, конец)``: момент начала входит в окно,
момент конца — уже нет. Так два соседних окна, идущих встык, не
перекрываются.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

__all__ = ["DailyWindow"]


@dataclass(frozen=True, slots=True)
class DailyWindow:
    """Ежедневный промежуток времени в конкретном часовом поясе."""

    start: time
    end: time
    timezone: str

    def __post_init__(self) -> None:
        if self.start == self.end:
            raise ValueError("daily window start and end must differ")
        # Пояс проверяется здесь: неверное имя должно обнаруживаться при
        # создании окна, а не в момент первой проверки времени.
        ZoneInfo(self.timezone)

    @property
    def crosses_midnight(self) -> bool:
        """Переходит ли окно через полночь."""
        return self.start > self.end

    def contains(self, moment: datetime) -> bool:
        """Попадает ли момент в окно.

        Момент приводится к часовому поясу окна, поэтому переход на
        летнее время и работа сервера в другом поясе на результат не
        влияют.
        """
        local = moment.astimezone(ZoneInfo(self.timezone)).timetz().replace(tzinfo=None)
        if self.crosses_midnight:
            return local >= self.start or local < self.end
        return self.start <= local < self.end

    def describe(self) -> str:
        """Окно одной строкой для диагностики и ответов оператору."""
        return f"{self.start:%H:%M}–{self.end:%H:%M} {self.timezone}"
