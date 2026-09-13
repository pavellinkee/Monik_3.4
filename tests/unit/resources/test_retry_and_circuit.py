"""Тесты retry-политики и circuit breaker."""

from __future__ import annotations

import random
from datetime import timedelta

import pytest

from monik.config.sections.resources import CircuitBreakerConfig, RetryConfig
from monik.domain.enums.resources import CircuitState
from monik.domain.errors import (
    AuthenticationError,
    DataError,
    RateLimitError,
    TimeoutError,
)
from monik.services.observability import FakeClock
from monik.services.resources import CircuitBreaker, RetryPolicy


class TestRetryPolicy:
    def _policy(self, **overrides: object) -> RetryPolicy:
        config = RetryConfig(
            max_attempts=3,
            initial_delay_seconds=1.0,
            max_delay_seconds=10.0,
            backoff_multiplier=2.0,
            jitter_ratio=0.0,
            **overrides,  # type: ignore[arg-type]
        )
        return RetryPolicy(config, rng=random.Random(1))

    def test_budget_is_limited(self) -> None:
        """Бесконечные повторы запрещены (CLAUDE.md §32)."""
        policy = self._policy()
        error = TimeoutError("slow").info
        assert policy.should_retry(error, attempts_used=1)
        assert policy.should_retry(error, attempts_used=2)
        assert not policy.should_retry(error, attempts_used=3)

    def test_non_retryable_errors_are_not_repeated(self) -> None:
        policy = self._policy()
        assert not policy.should_retry(DataError("bad payload").info, attempts_used=0)
        assert not policy.should_retry(AuthenticationError("no key").info, attempts_used=0)

    def test_exponential_backoff(self) -> None:
        policy = self._policy()
        error = TimeoutError("slow").info
        assert policy.delay_for(error, attempts_used=0) == 1.0
        assert policy.delay_for(error, attempts_used=1) == 2.0
        assert policy.delay_for(error, attempts_used=2) == 4.0

    def test_delay_is_capped(self) -> None:
        policy = self._policy()
        assert policy.delay_for(TimeoutError("slow").info, attempts_used=10) == 10.0

    def test_jitter_stays_within_bounds(self) -> None:
        config = RetryConfig(
            initial_delay_seconds=1.0,
            max_delay_seconds=10.0,
            backoff_multiplier=2.0,
            jitter_ratio=0.5,
        )
        policy = RetryPolicy(config, rng=random.Random(7))
        error = TimeoutError("slow").info
        for _ in range(50):
            delay = policy.delay_for(error, attempts_used=1)
            assert 1.0 <= delay <= 2.0

    def test_jitter_produces_varied_delays(self) -> None:
        """Одновременные повторы не должны совпадать по времени (12 §26)."""
        config = RetryConfig(jitter_ratio=0.5, initial_delay_seconds=1.0)
        policy = RetryPolicy(config, rng=random.Random(7))
        error = TimeoutError("slow").info
        delays = {policy.delay_for(error, attempts_used=1) for _ in range(20)}
        assert len(delays) > 1

    def test_retry_after_takes_precedence(self) -> None:
        """Указание провайдера важнее расчётной задержки (12 §27)."""
        policy = self._policy()
        error = RateLimitError("slow down", retry_after=timedelta(seconds=42)).info
        assert policy.delay_for(error, attempts_used=0) == 42.0

    def test_retry_after_can_be_disabled_only_by_invalid_config(self) -> None:
        with pytest.raises(ValueError, match="respect_retry_after"):
            RetryConfig(respect_retry_after=False)

    def test_rate_limit_is_retryable(self) -> None:
        policy = self._policy()
        assert policy.should_retry(RateLimitError("429").info, attempts_used=0)


class TestRateLimitCooldown:
    """Исчерпанная квота — не временный сбой.

    Временный сбой проходит за секунды, а суточная квота
    восстанавливается часами. Пробы каждые тридцать секунд всё это время
    только расходуют её остаток, поэтому для этой категории отказа паузы
    растут по лестнице.
    """

    def _breaker(self, clock: FakeClock) -> CircuitBreaker:
        return CircuitBreaker(
            CircuitBreakerConfig(
                failure_threshold=1,
                recovery_timeout_seconds=30.0,
                half_open_max_calls=1,
                success_threshold=1,
            ),
            clock,
        )

    def _probe(self, breaker: CircuitBreaker, clock: FakeClock, seconds: float) -> bool:
        """Прошла ли пауза указанной длины: состояние после ожидания."""
        clock.advance(timedelta(seconds=seconds))
        return breaker.state is CircuitState.HALF_OPEN

    def test_first_ten_probes_keep_the_short_pause(self, clock: FakeClock) -> None:
        breaker = self._breaker(clock)
        for _ in range(10):
            breaker.on_failure(rate_limited=True)
            assert not self._probe(breaker, clock, 29)
            assert self._probe(breaker, clock, 2)

    def test_pause_grows_after_ten_probes(self, clock: FakeClock) -> None:
        """Одиннадцатая проба ждёт пять минут, а не тридцать секунд."""
        breaker = self._breaker(clock)
        for _ in range(10):
            breaker.on_failure(rate_limited=True)
            clock.advance(timedelta(seconds=31))
            assert breaker.state is CircuitState.HALF_OPEN

        breaker.on_failure(rate_limited=True)

        assert not self._probe(breaker, clock, 60)
        assert self._probe(breaker, clock, 245)

    def test_pause_reaches_hours(self, clock: FakeClock) -> None:
        """После лестницы пробы идут раз в два часа и не прекращаются."""
        breaker = self._breaker(clock)
        for _ in range(22):
            breaker.on_failure(rate_limited=True)
            clock.advance(timedelta(hours=3))
            assert breaker.state is CircuitState.HALF_OPEN

        breaker.on_failure(rate_limited=True)

        assert not self._probe(breaker, clock, 3600)
        assert self._probe(breaker, clock, 3601)

    def test_ordinary_failure_keeps_the_configured_pause(self, clock: FakeClock) -> None:
        """Лестница не касается обычных отказов."""
        breaker = self._breaker(clock)
        for _ in range(15):
            breaker.on_failure()
            clock.advance(timedelta(seconds=31))
            assert breaker.state is CircuitState.HALF_OPEN

    def test_ordinary_failure_resets_the_ladder(self, clock: FakeClock) -> None:
        """Исправный ресурс не наследует многочасовую паузу от квоты."""
        breaker = self._breaker(clock)
        for _ in range(12):
            breaker.on_failure(rate_limited=True)
            clock.advance(timedelta(minutes=6))
            assert breaker.state is CircuitState.HALF_OPEN

        breaker.on_failure()

        assert self._probe(breaker, clock, 31)

    def test_success_resets_the_ladder(self, clock: FakeClock) -> None:
        breaker = self._breaker(clock)
        for _ in range(12):
            breaker.on_failure(rate_limited=True)
            clock.advance(timedelta(minutes=6))
        assert breaker.state is CircuitState.HALF_OPEN
        breaker.on_success()
        assert breaker.state is CircuitState.CLOSED

        breaker.on_failure(rate_limited=True)

        assert self._probe(breaker, clock, 31)


class TestCircuitBreaker:
    def _breaker(self, clock: FakeClock, **overrides: object) -> CircuitBreaker:
        settings: dict[str, object] = {
            "failure_threshold": 3,
            "recovery_timeout_seconds": 30.0,
            "half_open_max_calls": 1,
            "success_threshold": 2,
        }
        settings.update(overrides)
        return CircuitBreaker(CircuitBreakerConfig(**settings), clock)  # type: ignore[arg-type]

    def test_starts_closed(self, clock: FakeClock) -> None:
        breaker = self._breaker(clock)
        assert breaker.state is CircuitState.CLOSED
        assert breaker.allows_request()

    def test_opens_after_threshold(self, clock: FakeClock) -> None:
        breaker = self._breaker(clock)
        for _ in range(3):
            breaker.on_failure()
        assert breaker.state is CircuitState.OPEN
        assert not breaker.allows_request()

    def test_single_failure_does_not_open(self, clock: FakeClock) -> None:
        breaker = self._breaker(clock)
        breaker.on_failure()
        assert breaker.state is CircuitState.CLOSED

    def test_success_resets_failures(self, clock: FakeClock) -> None:
        breaker = self._breaker(clock)
        breaker.on_failure()
        breaker.on_failure()
        breaker.on_success()
        breaker.on_failure()
        assert breaker.state is CircuitState.CLOSED

    def test_half_open_after_recovery_timeout(self, clock: FakeClock) -> None:
        breaker = self._breaker(clock)
        for _ in range(3):
            breaker.on_failure()
        clock.advance(timedelta(seconds=31))
        assert breaker.state is CircuitState.HALF_OPEN
        assert breaker.allows_request()

    def test_half_open_limits_probe_calls(self, clock: FakeClock) -> None:
        breaker = self._breaker(clock)
        for _ in range(3):
            breaker.on_failure()
        clock.advance(timedelta(seconds=31))
        assert breaker.try_acquire()
        assert not breaker.try_acquire()
        assert not breaker.allows_request()

    def test_probe_slot_is_claimed_atomically(self, clock: FakeClock) -> None:
        """Разрешение и занятие слота — одна операция.

        Регрессия: проверка выполнялась до очереди, а слот занимался
        после неё, и в ``HALF_OPEN`` разрешение успевали получить все
        накопившиеся запросы.
        """
        breaker = self._breaker(clock, half_open_max_calls=2)
        for _ in range(3):
            breaker.on_failure()
        clock.advance(timedelta(seconds=31))

        granted = [breaker.try_acquire() for _ in range(10)]

        assert granted.count(True) == 2, "лимит одновременных проб обязан соблюдаться"

    def test_slot_is_released_without_counting_an_outcome(self, clock: FakeClock) -> None:
        """Проба, завершившаяся без учёта исхода, освобождает слот.

        Регрессия: ошибка, для которой счётчик отказов не ведётся,
        занимала слот навсегда, и при ``half_open_max_calls = 1`` ресурс
        оставался в ``HALF_OPEN`` без единого разрешённого запроса.
        """
        breaker = self._breaker(clock)
        for _ in range(3):
            breaker.on_failure()
        clock.advance(timedelta(seconds=31))

        assert breaker.try_acquire()
        breaker.release()

        assert breaker.state is CircuitState.HALF_OPEN
        assert breaker.try_acquire(), "слот обязан освободиться"

    def test_half_open_failure_reopens(self, clock: FakeClock) -> None:
        breaker = self._breaker(clock)
        for _ in range(3):
            breaker.on_failure()
        clock.advance(timedelta(seconds=31))
        assert breaker.try_acquire()
        breaker.on_failure()
        breaker.release()
        assert breaker.state is CircuitState.OPEN

    def test_half_open_closes_after_successes(self, clock: FakeClock) -> None:
        """Полный цикл восстановления в том порядке, в котором его
        выполняет Resource Manager: слот занимается ``try_acquire`` перед
        обращением и освобождается ``release`` после него
        (``05_RESOURCE_MANAGER.md`` §68).

        Без освобождения слота после удачной пробы breaker при
        ``half_open_max_calls = 1`` и ``success_threshold = 2`` навсегда
        оставался бы в ``HALF_OPEN``.
        """
        breaker = self._breaker(clock)
        for _ in range(3):
            breaker.on_failure()
        assert breaker.state is CircuitState.OPEN

        clock.advance(timedelta(seconds=31))
        assert breaker.state is CircuitState.HALF_OPEN

        assert breaker.try_acquire()
        breaker.on_success()
        breaker.release()
        assert breaker.state is CircuitState.HALF_OPEN

        assert breaker.try_acquire(), "второй пробе нужен свободный слот"
        breaker.on_success()
        breaker.release()
        assert breaker.state is CircuitState.CLOSED
        assert breaker.allows_request()

    @pytest.mark.parametrize("half_open_max_calls", [1, 2, 3])
    @pytest.mark.parametrize("success_threshold", [1, 2, 3])
    def test_recovery_is_reachable_for_any_configuration(
        self, clock: FakeClock, half_open_max_calls: int, success_threshold: int
    ) -> None:
        """Ни одно сочетание порогов не делает восстановление невозможным.

        Регрессия: при ``half_open_max_calls < success_threshold`` breaker
        запирался в ``HALF_OPEN`` и ресурс не восстанавливался никогда.
        """
        breaker = self._breaker(
            clock,
            half_open_max_calls=half_open_max_calls,
            success_threshold=success_threshold,
        )
        for _ in range(3):
            breaker.on_failure()
        clock.advance(timedelta(seconds=31))

        for _ in range(success_threshold):
            assert breaker.try_acquire()
            breaker.on_success()
            breaker.release()
        assert breaker.state is CircuitState.CLOSED

    def test_half_open_probe_failure_reopens_and_recovery_stays_possible(
        self, clock: FakeClock
    ) -> None:
        """Неудачная проба возвращает OPEN, но не запирает ресурс."""
        breaker = self._breaker(clock)
        for _ in range(3):
            breaker.on_failure()
        clock.advance(timedelta(seconds=31))
        assert breaker.try_acquire()
        breaker.on_failure()
        breaker.release()
        assert breaker.state is CircuitState.OPEN

        clock.advance(timedelta(seconds=31))
        for _ in range(2):
            assert breaker.try_acquire()
            breaker.on_success()
            breaker.release()
        assert breaker.state is CircuitState.CLOSED

    def test_disabled_breaker_always_allows(self, clock: FakeClock) -> None:
        breaker = self._breaker(clock, enabled=False)
        for _ in range(10):
            breaker.on_failure()
        assert breaker.allows_request()
