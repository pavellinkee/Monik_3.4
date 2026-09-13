"""Получение и валидация котировок Level 1.

Все внешние запросы выполняются через Adapter, а тот — через Resource
Manager (``02_LEVEL1_SCANNER.md`` §11, ``10_LEVEL_1_SCANNER.md`` §17).
Scanner не выполняет HTTP-запросов сам и не знает деталей API провайдера.

Количество одновременных запросов ограничено: бесконечное число
asynchronous tasks запрещено (``02_LEVEL1_SCANNER.md`` §60).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta

from monik.domain.enums.errors import ErrorCategory
from monik.domain.enums.operations import OperationType
from monik.domain.enums.providers import ProviderId
from monik.domain.enums.resources import RequestPriority
from monik.domain.errors import MonikError
from monik.domain.models.capability import CapabilityKey
from monik.domain.models.quote import Quote
from monik.domain.models.token import Token
from monik.domain.value_objects.amounts import TokenAmount
from monik.domain.value_objects.identifiers import RequestId, ScanId
from monik.domain.value_objects.identity import NetworkId
from monik.infrastructure.providers.contract import AggregatorAdapter, QuoteRequest
from monik.services.level1.filters import capability_operation
from monik.services.level1.no_route import NoRouteMemory
from monik.services.level1.validation import quote_rejection_reason
from monik.services.observability.clock import Clock
from monik.services.observability.context import log_context
from monik.services.observability.logging import get_logger, log_fields

__all__ = ["QuoteAttempt", "QuoteCollector", "QuoteStatistics"]

_LOGGER = get_logger("services.level1.quotes")

#: Категории, означающие «маршрута сейчас нет». Провайдер ответил
#: корректно, поэтому повторять запрос в следующем цикле смысла мало.
_NEGATIVE_ROUTE_CATEGORIES = frozenset({ErrorCategory.NO_ROUTE, ErrorCategory.ROUTE_REJECTED})

#: Приоритет запроса по направлению. Готовая SELL-проверка обслуживается
#: раньше незавершённой BUY-проверки (``CLAUDE.md`` §15).
_PRIORITIES: dict[OperationType, RequestPriority] = {
    OperationType.BUY: RequestPriority.LEVEL1_BUY,
    OperationType.SELL: RequestPriority.LEVEL1_SELL,
}


@dataclass(frozen=True, slots=True)
class QuoteAttempt:
    """Итог одной попытки получить котировку.

    Ошибка одного провайдера не останавливает цикл
    (``02_LEVEL1_SCANNER.md`` §51): она сохраняется здесь и учитывается
    в статистике.
    """

    provider_id: ProviderId
    operation: OperationType
    quote: Quote | None = None
    rejection_reason: str | None = None
    error_category: ErrorCategory | None = None
    error_message: str | None = None

    @property
    def is_usable(self) -> bool:
        """Можно ли использовать котировку в сравнении."""
        return self.quote is not None and self.rejection_reason is None


@dataclass(slots=True)
class QuoteStatistics:
    """Счётчики запросов одного цикла."""

    #: Запросы, действительно отправленные провайдерам.
    requests: int = 0
    successful: int = 0
    #: Провайдер ответил отказом: нет маршрута, отклонённый маршрут, сбой.
    failed: int = 0
    skipped: int = 0
    #: Запрос не отправлен: ресурс закрыт предохранителем или очередь не
    #: дождалась. Это состояние Monik, а не ответ провайдера, поэтому в
    #: успешность оно не входит — иначе открытый circuit breaker выглядел
    #: бы как отказ агрегатора.
    refused: int = 0
    attempts: list[QuoteAttempt] = field(default_factory=list)


class QuoteCollector:
    """Запрашивает котировки у адаптеров с ограниченной конкурентностью."""

    def __init__(
        self,
        adapters: dict[ProviderId, AggregatorAdapter],
        clock: Clock,
        *,
        scan_id: ScanId,
        max_age: timedelta,
        max_concurrent: int,
        no_route: NoRouteMemory,
        request_timeout: timedelta | None = None,
    ) -> None:
        self._adapters = adapters
        self._clock = clock
        self._no_route = no_route
        self._scan_id = scan_id
        self._max_age = max_age
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._request_timeout = request_timeout
        self.statistics = QuoteStatistics()

    async def fetch(
        self,
        provider_id: ProviderId,
        *,
        network_id: NetworkId,
        operation: OperationType,
        input_token: Token,
        output_token: Token,
        input_amount: TokenAmount,
    ) -> QuoteAttempt:
        """Получить и проверить одну котировку."""
        adapter = self._adapters[provider_id]
        request = QuoteRequest(
            network_id=network_id,
            operation=operation,
            input_token=input_token,
            output_token=output_token,
            input_amount=input_amount,
            request_id=RequestId.generate(),
            priority=_PRIORITIES[operation],
            timeout=self._request_timeout,
        )
        with log_context(
            scan_id=str(self._scan_id),
            request_id=str(request.request_id),
            provider=provider_id.value,
            network=str(network_id),
            operation=operation.value,
            # Без пары запись об отказе не позволяет понять, какая
            # комбинация отвергнута: провайдер отвечает по-разному на
            # разные токены, и причину приходится искать перебором
            # (``CLAUDE.md`` §48). Контекст, а не одна запись, — чтобы
            # пара попадала и в записи повторов Resource Manager.
            input_token=str(input_token.key),
            output_token=str(output_token.key),
        ):
            return await self._fetch(adapter, request)

    @staticmethod
    def _capability_key(provider_id: ProviderId, request: QuoteRequest) -> CapabilityKey:
        """Комбинация, к которой относится запрос.

        Ключ строится по промежуточному токену — тому, ликвидность
        которого и определяет наличие маршрута. Для BUY это выходной
        токен, для SELL — входной.
        """
        token = (
            request.output_token if request.operation is OperationType.BUY else request.input_token
        )
        return CapabilityKey(
            provider_id=provider_id,
            network_id=request.network_id,
            operation=capability_operation(request.operation),
            token=token.key,
        )

    def record_skipped(self, count: int = 1) -> None:
        """Учесть комбинацию, для которой запрос не выполнялся (§89)."""
        self.statistics.skipped += count

    async def _fetch(self, adapter: AggregatorAdapter, request: QuoteRequest) -> QuoteAttempt:
        async with self._semaphore:
            try:
                quote = await adapter.get_quote(request)
            except MonikError as error:
                if error.info.category is ErrorCategory.RESOURCE:
                    # Запрос не покинул Monik: ресурс закрыт или очередь не
                    # дождалась. К ответам провайдера это не относится.
                    self.statistics.refused += 1
                    attempt = QuoteAttempt(
                        provider_id=adapter.provider_id,
                        operation=request.operation,
                        error_category=error.info.category,
                        error_message=error.info.message,
                    )
                    self.statistics.attempts.append(attempt)
                    _LOGGER.info(
                        "quote request was not sent",
                        extra=log_fields(
                            error_code=error.info.code,
                            detail=error.info.message,
                        ),
                    )
                    return attempt
                self.statistics.requests += 1
                self.statistics.failed += 1
                attempt = QuoteAttempt(
                    provider_id=adapter.provider_id,
                    operation=request.operation,
                    error_category=error.info.category,
                    error_message=error.info.message,
                )
                self.statistics.attempts.append(attempt)
                _LOGGER.warning(
                    "quote request failed",
                    extra=log_fields(
                        error_category=error.info.category.value,
                        error_code=error.info.code,
                        # Без статуса и пояснения провайдера код вида
                        # ``http_client_error`` не даёт понять, что именно
                        # отвергнуто. Сообщение уже отредактировано
                        # адаптером (``22_SECURITY.md``).
                        http_status=error.info.http_status,
                        detail=error.info.message,
                    ),
                )
                if error.info.category in _NEGATIVE_ROUTE_CATEGORIES:
                    self._no_route.remember(self._capability_key(adapter.provider_id, request))
                return attempt

        reason = quote_rejection_reason(
            quote,
            request,
            provider_id=adapter.provider_id,
            now=self._clock.now(),
            max_age=self._max_age,
        )
        if reason is not None:
            self.statistics.requests += 1
            self.statistics.failed += 1
            attempt = QuoteAttempt(
                provider_id=adapter.provider_id,
                operation=request.operation,
                quote=quote,
                rejection_reason=reason,
            )
            self.statistics.attempts.append(attempt)
            _LOGGER.info("quote rejected", extra=log_fields(reason=reason))
            return attempt

        self.statistics.requests += 1
        self.statistics.successful += 1
        self._no_route.forget(self._capability_key(adapter.provider_id, request))
        attempt = QuoteAttempt(
            provider_id=adapter.provider_id,
            operation=request.operation,
            quote=quote,
        )
        self.statistics.attempts.append(attempt)
        return attempt
