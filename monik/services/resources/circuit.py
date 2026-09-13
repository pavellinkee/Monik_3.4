"""Circuit breaker для внешних ресурсов.

Состояния ``CLOSED`` / ``OPEN`` / ``HALF_OPEN`` (``CLAUDE.md`` §33,
``12_RESOURCE_MANAGER.md`` §31-35).

Circuit breaker отражает **временную** недоступность и не изменяет
Capability Registry (``05_RESOURCE_MANAGER.md`` §11): временный сбой не
означает отсутствие поддержки операции.
"""

from __future__ import annotations

from monik.config.sections.resources import CircuitBreakerConfig, CooldownStep
from monik.domain.enums.resources import CircuitState
from monik.services.observability.clock import Clock

__all__ = ["CircuitBreaker"]


class CircuitBreaker:
    """Ограничивает поток запросов к недоступному ресурсу."""

    def __init__(self, config: CircuitBreakerConfig, clock: Clock) -> None:
        self._config = config
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._successes = 0
        self._opened_at: float | None = None
        self._half_open_calls = 0
        #: Сколько раз подряд ресурс открывался из-за исчерпанного лимита.
        #: Пауза перед следующей пробой растёт вместе с этим счётчиком.
        self._rate_limit_openings = 0

    @property
    def state(self) -> CircuitState:
        """Текущее состояние с учётом истёкшего времени восстановления."""
        if self._state is CircuitState.OPEN and self._recovery_elapsed():
            self._state = CircuitState.HALF_OPEN
            self._half_open_calls = 0
            self._successes = 0
        return self._state

    def allows_request(self) -> bool:
        """Есть ли смысл ставить запрос в очередь.

        Быстрый ответ без побочных эффектов: он избавляет от ожидания в
        очереди заведомо обречённые запросы. Разрешение на выполнение
        выдаёт :meth:`try_acquire` — непосредственно перед обращением к
        ресурсу.
        """
        if not self._config.enabled:
            return True
        state = self.state
        if state is CircuitState.CLOSED:
            return True
        if state is CircuitState.OPEN:
            return False
        return self._half_open_calls < self._config.half_open_max_calls

    def try_acquire(self) -> bool:
        """Разрешить выполнение и занять слот пробы, если он нужен.

        Проверка и занятие слота выполняются **одной операцией**. Иначе
        между ними проходит очередь и пауза по частоте, и в ``HALF_OPEN``
        разрешение успевают получить все накопившиеся запросы: лимит
        ``half_open_max_calls`` перестаёт что-либо ограничивать, а
        восстанавливающийся ресурс получает всю очередь разом
        (``12_RESOURCE_MANAGER.md`` §34).

        В ``HALF_OPEN`` ограничивается число **одновременных** проб, а не
        их общее количество: при ``half_open_max_calls = 1`` и
        ``success_threshold = 2`` суммарный счёт сделал бы закрытие
        недостижимым вопреки §68. Слот освобождает :meth:`release`.
        """
        if not self._config.enabled:
            return True
        state = self.state
        if state is CircuitState.CLOSED:
            return True
        if state is CircuitState.OPEN:
            return False
        if self._half_open_calls >= self._config.half_open_max_calls:
            return False
        self._half_open_calls += 1
        return True

    def release(self) -> None:
        """Освободить слот пробы.

        Вызывается при **любом** завершении операции, включая ошибку, для
        которой счётчик отказов не ведётся. Иначе одна такая проба
        занимала бы слот навсегда, и ресурс оставался бы в ``HALF_OPEN``
        без единого разрешённого запроса.
        """
        self._half_open_calls = max(0, self._half_open_calls - 1)

    def on_success(self) -> None:
        """Учесть успешную операцию."""
        if not self._config.enabled:
            return
        # Обращение к ``state`` применяет отложенный переход OPEN -> HALF_OPEN,
        # если время восстановления уже истекло.
        if self.state is CircuitState.HALF_OPEN:
            self._successes += 1
            if self._successes >= self._config.success_threshold:
                self._close()
            return
        self._failures = 0

    def on_failure(self, *, rate_limited: bool = False) -> None:
        """Учесть неуспешную операцию.

        ``rate_limited`` означает, что ресурс отказал именно по лимиту
        запросов. Это не временный сбой: квота восстанавливается не
        секундами, а часами, и частые пробы всё это время только
        расходуют её остаток. Такой отказ удлиняет паузу перед следующей
        пробой по лестнице охлаждения, остальные — нет.
        """
        if not self._config.enabled:
            return
        if self.state is CircuitState.HALF_OPEN:
            self._open(rate_limited=rate_limited)
            return
        self._failures += 1
        if self._failures >= self._config.failure_threshold:
            self._open(rate_limited=rate_limited)

    def _open(self, *, rate_limited: bool = False) -> None:
        if rate_limited:
            self._rate_limit_openings += 1
        else:
            # Обычный отказ означает, что дело уже не в квоте: лестница
            # начинается заново, иначе исправный ресурс наследовал бы
            # многочасовую паузу от давнего превышения лимита.
            self._rate_limit_openings = 0
        self._state = CircuitState.OPEN
        self._opened_at = self._clock.monotonic()
        self._failures = 0
        self._successes = 0
        self._half_open_calls = 0

    def _close(self) -> None:
        self._state = CircuitState.CLOSED
        self._opened_at = None
        self._failures = 0
        self._successes = 0
        self._half_open_calls = 0
        self._rate_limit_openings = 0

    def _recovery_elapsed(self) -> bool:
        if self._opened_at is None:
            return True
        elapsed = self._clock.monotonic() - self._opened_at
        return elapsed >= self._cooldown_seconds()

    def _cooldown_seconds(self) -> float:
        """Пауза до следующей пробы.

        Для обычного отказа она постоянна. Для исчерпанного лимита берётся
        очередная ступень лестницы: первые пробы частые — ограничение
        могло оказаться мгновенным, — дальше всё реже. Последняя ступень
        повторяется, поэтому попытки не прекращаются совсем и ресурс
        вернётся в строй сам, когда квота восстановится.
        """
        if self._rate_limit_openings == 0:
            return self._config.recovery_timeout_seconds
        return _step_seconds(
            self._config.rate_limit_cooldown,
            self._rate_limit_openings,
            default=self._config.recovery_timeout_seconds,
        )


def _step_seconds(ladder: tuple[CooldownStep, ...], opening: int, *, default: float) -> float:
    """Пауза для ``opening``-го подряд открытия (нумерация с единицы)."""
    if not ladder:
        return default
    remaining = opening
    for step in ladder:
        if remaining <= step.attempts:
            return step.seconds
        remaining -= step.attempts
    return ladder[-1].seconds
