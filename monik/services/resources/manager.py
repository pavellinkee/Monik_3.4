"""Resource Manager — единственная точка контроля внешних запросов.

Все внешние запросы проходят через него (``CLAUDE.md`` §14): Level 1,
Level 2, Fee System, Capability discovery, Telegram и maintenance не
обращаются к сети напрямую.

Он объединяет (``05_RESOURCE_MANAGER.md``, ``12_RESOURCE_MANAGER.md``):
приоритетную очередь, concurrency limits, rate limits, timeout, retry с
backoff и jitter, circuit breaker, дедупликацию, backpressure, отмену и
метрики задержек.
"""

from __future__ import annotations

import asyncio
import builtins
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

from monik.config.sections.resources import ResourceConfig
from monik.domain.enums.errors import ErrorCategory
from monik.domain.enums.resources import CircuitState, ResourceResultStatus
from monik.domain.errors import MonikError, ResourceError, TimeoutError
from monik.domain.errors.base import ErrorInfo
from monik.domain.errors.classification import RETRYABLE_CATEGORIES
from monik.domain.models.resource import ResourceKey, ResourceRequest, ResourceResult
from monik.services.observability.clock import Clock
from monik.services.observability.logging import get_logger, log_fields
from monik.services.resources.circuit import CircuitBreaker
from monik.services.resources.dedup import InFlightRegistry
from monik.services.resources.gate import PriorityGate
from monik.services.resources.limits import RateLimiter, ResourceLimits
from monik.services.resources.retry import RetryPolicy

__all__ = ["QueueSnapshot", "ResourceManager", "Sleeper"]

_LOGGER = get_logger("services.resources")

#: Функция ожидания. Выделена, чтобы тесты управляли временем детерминированно.
Sleeper = Callable[[float], Awaitable[None]]

#: Ключ глобального лимита конкурентности.
_GLOBAL_GATE = "__global__"


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
    """Состояние одной очереди ресурса."""

    resource: str
    active: int
    waiting: int
    max_concurrent: int
    requests_per_second: float
    circuit_state: CircuitState


class ResourceManager:
    """Выполняет внешние операции в рамках установленных ограничений."""

    def __init__(
        self,
        config: ResourceConfig,
        clock: Clock,
        *,
        limits: dict[str, ResourceLimits] | None = None,
        sleeper: Sleeper | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._config = config
        self._clock = clock
        self._limits = dict(limits or {})
        self._sleep: Sleeper = sleeper or asyncio.sleep
        self._retry = RetryPolicy(config.retry, rng=rng)
        self._gates: dict[str, PriorityGate] = {
            _GLOBAL_GATE: PriorityGate(
                limit=config.global_max_concurrent_requests,
                capacity=config.queue_capacity,
                name="global",
            )
        }
        self._rate_limiters: dict[str, RateLimiter] = {}
        self._breakers: dict[str, CircuitBreaker] = {}
        self._dedup = InFlightRegistry()
        self._results: list[ResourceResult] = []

    # --- публичный API -----------------------------------------------------

    async def execute[T](
        self, request: ResourceRequest, operation: Callable[[], Awaitable[T]]
    ) -> T:
        """Выполнить операцию под контролем менеджера.

        Возвращает результат операции. Все ошибки нормализованы: наружу
        не выходят исключения библиотек.
        """
        if request.deduplication_key and self._config.deduplicate_in_flight:
            return await self._dedup.run(
                request.deduplication_key, lambda: self._execute_controlled(request, operation)
            )
        return await self._execute_controlled(request, operation)

    def register_limits(self, key: ResourceKey, limits: ResourceLimits) -> None:
        """Задать лимиты конкретного ресурса."""
        self._limits[str(key)] = limits

    def circuit_state(self, key: ResourceKey) -> CircuitState:
        """Состояние circuit breaker ресурса."""
        return self._breaker(str(key)).state

    def results(self) -> tuple[ResourceResult, ...]:
        """Метрики выполненных операций."""
        return tuple(self._results)

    @property
    def merged_requests(self) -> int:
        """Сколько запросов было объединено дедупликацией."""
        return self._dedup.merged_count

    def queue_depth(self) -> int:
        """Сколько запросов ожидает глобального разрешения."""
        return self._gates[_GLOBAL_GATE].waiting

    def queue_snapshots(self) -> tuple[QueueSnapshot, ...]:
        """Состояние очередей с заданными лимитами.

        Нужно для диагностики и команды состояния агрегатора: оператор
        должен видеть, какая очередь загружена и какой лимит к ней
        применяется (``05_RESOURCE_MANAGER.md`` §69).
        """
        snapshots = []
        for name, limits in sorted(self._limits.items()):
            gate = self._gates.get(name)
            snapshots.append(
                QueueSnapshot(
                    resource=name,
                    active=gate.active if gate else 0,
                    waiting=gate.waiting if gate else 0,
                    max_concurrent=limits.max_concurrent,
                    requests_per_second=limits.requests_per_second,
                    circuit_state=self._widest_circuit_state(name),
                )
            )
        return tuple(snapshots)

    def _widest_circuit_state(self, scope: str) -> CircuitState:
        """Наихудшее состояние breaker'ов внутри области.

        Breaker живёт на уровне конкретного endpoint (``05`` §50), поэтому
        у одной очереди их может быть несколько: показывается самое
        серьёзное.
        """
        states = [
            breaker.state
            for name, breaker in self._breakers.items()
            if name == scope or name.startswith(f"{scope}/")
        ]
        for candidate in (CircuitState.OPEN, CircuitState.HALF_OPEN):
            if candidate in states:
                return candidate
        return CircuitState.CLOSED

    # --- выполнение --------------------------------------------------------

    async def _execute_controlled[T](
        self, request: ResourceRequest, operation: Callable[[], Awaitable[T]]
    ) -> T:
        resource = str(request.key)
        breaker = self._breaker(resource)
        if not breaker.allows_request():
            # Быстрый отказ до очереди: обречённый запрос не должен
            # занимать место и расходовать бюджет частоты. Разрешение
            # выдаётся позже, непосредственно перед выполнением.
            raise self._circuit_open(request, resource)

        queue_started = time.monotonic()
        acquired: list[PriorityGate] = []
        try:
            # Сначала очередь ресурса: она задаёт порядок обслуживания и
            # ограничивает одновременные обращения к этому агрегатору.
            for gate in self._gates_for(request.key):
                await gate.acquire(request, timeout=self._config.queue_wait_timeout_seconds)
                acquired.append(gate)
            # Пауза по частоте выдерживается до захвата общего потолка:
            # ожидающий своей доли частоты запрос ничего не выполняет и
            # места выполняющегося занимать не должен. Иначе общий
            # потолок стал бы вторым ограничением частоты и удерживал
            # каждого провайдера ниже настроенного значения
            # (``05_RESOURCE_MANAGER.md`` §21).
            await self._await_rate_limit(request)
            global_gate = self._gates[_GLOBAL_GATE]
            await global_gate.acquire(request, timeout=self._config.queue_wait_timeout_seconds)
            acquired.append(global_gate)
            queued_for = time.monotonic() - queue_started
            # Состояние breaker'а могло измениться, пока запрос стоял в
            # очереди и ждал своей доли частоты. Разрешение выдаётся здесь,
            # одной операцией с занятием слота пробы: иначе в ``HALF_OPEN``
            # его получили бы все накопившиеся запросы разом.
            if not breaker.try_acquire():
                raise self._circuit_open(request, resource)
            try:
                return await self._run_with_retry(request, operation, breaker, queued_for)
            finally:
                breaker.release()
        finally:
            for gate in reversed(acquired):
                gate.release()

    @staticmethod
    def _circuit_open(request: ResourceRequest, resource: str) -> ResourceError:
        """Отказ из-за открытого circuit breaker."""
        return ResourceError(
            f"circuit breaker is open for {resource}",
            code="resource_circuit_open",
            request_id=request.request_id,
            operation=resource,
        )

    async def _run_with_retry[T](
        self,
        request: ResourceRequest,
        operation: Callable[[], Awaitable[T]],
        breaker: CircuitBreaker,
        queued_for: float,
    ) -> T:
        attempts = 0
        started = time.monotonic()
        while True:
            if attempts:
                # Первая попытка уже оплачена до захвата общего потолка;
                # повтор — это отдельный запрос и стоит отдельного места
                # в бюджете частоты.
                await self._await_rate_limit(request)
            attempts += 1
            try:
                result = await asyncio.wait_for(
                    operation(), timeout=request.timeout.total_seconds()
                )
            except asyncio.CancelledError:
                self._record(request, ResourceResultStatus.CANCELLED, queued_for, started, attempts)
                raise
            except builtins.TimeoutError as exc:
                error = TimeoutError(
                    f"operation on {request.key} exceeded {request.timeout}",
                    code="resource_timeout",
                    request_id=request.request_id,
                    operation=str(request.key),
                )
                if not self._retry.should_retry(error.info, attempts_used=attempts):
                    self._fail_breaker(breaker, error.info)
                    self._record(
                        request, ResourceResultStatus.TIMEOUT, queued_for, started, attempts
                    )
                    raise error from exc
                await self._backoff(request, error.info, attempts)
            except MonikError as error:
                if error.info.category.value == "cancellation":
                    self._record(
                        request, ResourceResultStatus.CANCELLED, queued_for, started, attempts
                    )
                    raise
                if not self._retry.should_retry(error.info, attempts_used=attempts):
                    self._fail_breaker(breaker, error.info)
                    self._record(
                        request,
                        self._failure_status(error.info),
                        queued_for,
                        started,
                        attempts,
                        error=error.info,
                    )
                    raise
                await self._backoff(request, error.info, attempts)
            else:
                breaker.on_success()
                self._record(request, ResourceResultStatus.SUCCESS, queued_for, started, attempts)
                return result

    @staticmethod
    def _fail_breaker(breaker: CircuitBreaker, error: ErrorInfo) -> None:
        """Учесть отказ логической операции в circuit breaker.

        Два правила, без которых breaker открывается ложно:

        * **одна логическая операция — один отказ.** Метод вызывается
          только после исчерпания retry budget, а не на каждой попытке:
          иначе три повтора одного запроса расходовали бы три пятых порога
          (``12_RESOURCE_MANAGER.md`` §33);
        * **отказом считается только недоступность ресурса.**
          Категории, для которых повтор бессмыслен по содержанию ответа
          (``DATA``, ``VALIDATION``, ``UNSUPPORTED``, ``AUTHENTICATION``),
          описывают запрос или данные, а не работоспособность провайдера:
          корректный ответ «для этой пары нет ликвидности» не является
          сбоем (``05_RESOURCE_MANAGER.md`` §29, ``19_HEALTH_MONITORING.md``
          §55).

        Классификация берётся из существующей модели ошибок
        (``18_ERROR_HANDLING.md`` §33-36): второго набора правил здесь не
        создаётся.
        """
        if error.category in RETRYABLE_CATEGORIES:
            breaker.on_failure(rate_limited=error.category is ErrorCategory.RATE_LIMIT)

    async def _await_rate_limit(self, request: ResourceRequest) -> None:
        """Дождаться разрешения rate limiter'а.

        Стоимость batch-запроса учитывается явно: несколько элементов не
        считаются одним запросом автоматически.
        """
        limiter = self._rate_limiter(request.key)
        if limiter is None:
            return
        # Стоимость больше стартового запаса допустима: резервация просто
        # отодвигает слот пропорционально. Ожидание конечно и предсказуемо.
        delay = limiter.reserve(request.batch_units)
        if delay > 0:
            await self._sleep(delay)

    async def _backoff(self, request: ResourceRequest, error: ErrorInfo, attempts: int) -> None:
        """Выдержать паузу перед следующей попыткой.

        Экспонента отсчитывается от числа уже завершившихся попыток минус
        текущая: после первого сбоя задержка равна начальной.
        """
        delay = self._retry.delay_for(error, attempts_used=attempts - 1)
        _LOGGER.info(
            "retrying resource operation",
            extra=log_fields(
                resource=str(request.key),
                attempt=attempts,
                delay_seconds=round(delay, 3),
                error_code=error.code,
            ),
        )
        if delay > 0:
            await self._sleep(delay)

    # --- вспомогательное ---------------------------------------------------

    def _gates_for(self, key: ResourceKey) -> tuple[PriorityGate, ...]:
        """Ворота ресурса от самых широких к самым узким.

        Общий потолок сюда не входит: он захватывается последним, уже
        после паузы по частоте. Порядок захвата одинаков для всех
        запросов, поэтому взаимная блокировка невозможна
        (``05_RESOURCE_MANAGER.md`` §44).

        Ворота создаются только для областей с заданными лимитами. Иначе
        запрос проходил бы через цепочку ворот, каждые из которых просто
        повторяют глобальный лимит: очередь провайдера должна быть одна и
        соответствовать реальной области ограничения
        (``05_RESOURCE_MANAGER.md`` §50-51).
        """
        gates = []
        for scope in (*key.parents(), key):
            name = str(scope)
            if name in self._limits:
                gates.append(self._gate(name))
        return tuple(gates)

    def _gate(self, name: str) -> PriorityGate:
        gate = self._gates.get(name)
        if gate is None:
            limits = self._limits.get(name)
            gate = PriorityGate(
                limit=limits.max_concurrent
                if limits
                else self._config.global_max_concurrent_requests,
                capacity=self._config.queue_capacity,
                name=name,
            )
            self._gates[name] = gate
        return gate

    def _rate_limiter(self, key: ResourceKey) -> RateLimiter | None:
        """Rate limiter самого узкого настроенного уровня."""
        for scope in (key, *reversed(key.parents())):
            name = str(scope)
            existing = self._rate_limiters.get(name)
            if existing is not None:
                return existing
            limits = self._limits.get(name)
            if limits is not None:
                limiter = RateLimiter(limits, self._clock)
                self._rate_limiters[name] = limiter
                return limiter
        return None

    def _breaker(self, name: str) -> CircuitBreaker:
        breaker = self._breakers.get(name)
        if breaker is None:
            breaker = CircuitBreaker(self._config.circuit_breaker, self._clock)
            self._breakers[name] = breaker
        return breaker

    @staticmethod
    def _failure_status(error: ErrorInfo) -> ResourceResultStatus:
        """Сопоставить категорию ошибки со статусом результата."""
        if error.category.value == "rate_limit":
            return ResourceResultStatus.RATE_LIMITED
        if error.category.value == "timeout":
            return ResourceResultStatus.TIMEOUT
        if error.category.value == "resource":
            return ResourceResultStatus.REJECTED
        return ResourceResultStatus.FAILURE

    def _record(
        self,
        request: ResourceRequest,
        status: ResourceResultStatus,
        queued_for: float,
        started: float,
        attempts: int,
        *,
        error: ErrorInfo | None = None,
    ) -> None:
        """Сохранить метрику выполнения."""
        self._results.append(
            ResourceResult(
                request_id=request.request_id,
                status=status,
                queued_for=timedelta(seconds=queued_for),
                executed_for=timedelta(seconds=time.monotonic() - started),
                attempts=attempts,
                finished_at=self._clock.now(),
                error_code=error.code if error else None,
                retry_after=error.retry_after if error else None,
            )
        )
