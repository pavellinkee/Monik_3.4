"""Очереди запросов по агрегаторам.

Проверяется путь из ``docs/provider_queues.md``:

``ProviderConfig`` → Resource Manager → очередь провайдера → rate limiter.

Отдельно проверяются изоляция агрегаторов, общий бюджет BUY/SELL и
Level 1/Level 2, порядок приоритетов и FIFO внутри приоритета.
"""

from __future__ import annotations

import asyncio
import copy
import random
from datetime import timedelta

import pytest

from monik.app.container import _register_provider_limits
from monik.config import parse_configuration
from monik.domain.enums.capability import CapabilityOperation
from monik.domain.enums.providers import ProviderId
from monik.domain.enums.resources import RequestPriority
from monik.domain.models.resource import ResourceKey
from monik.services.observability import FakeClock
from monik.services.resources import ResourceLimits, ResourceManager
from tests import factories as f
from tests.component.level1.conftest import level1_document
from tests.unit.config.conftest import VALID_ENV

from .conftest import ControlledSleeper, request, resource_config

#: Настроенные частоты из production-конфигурации.
ZERO_X_RPS = 4.9
VELORA_RPS = 4.9
UNISWAP_RPS = 5.9


def _configuration(**resources: object) -> object:
    """Конфигурация с тремя включёнными агрегаторами."""
    document = copy.deepcopy(level1_document())
    if resources:
        document["resources"] = {**document.get("resources", {}), **resources}
    document["providers"] = [
        {
            "provider_id": "zero_x",
            "enabled": True,
            "api_key": {"env": "MONIK_ZEROX_API_KEY"},
            "supported_networks": ["polygon"],
            "requests_per_second": ZERO_X_RPS,
            "max_concurrent_requests": 4,
        },
        {
            "provider_id": "velora",
            "enabled": True,
            "api_key": {"env": "MONIK_VELORA_API_KEY"},
            "supported_networks": ["polygon"],
            "requests_per_second": VELORA_RPS,
            "max_concurrent_requests": 4,
        },
        {
            "provider_id": "uniswap",
            "enabled": True,
            "api_key": {"env": "MONIK_UNISWAP_API_KEY"},
            "supported_networks": ["polygon"],
            "requests_per_second": UNISWAP_RPS,
            "max_concurrent_requests": 2,
            "options": {"swapper": "0x0000000000000000000000000000000000000A11"},
        },
    ]
    environ = dict(VALID_ENV)
    environ["MONIK_VELORA_API_KEY"] = "velora-test-key"
    environ["MONIK_UNISWAP_API_KEY"] = "uniswap-test-key"
    return parse_configuration(document, environ=environ).config


async def _ok(value: str = "ok") -> str:
    return value


class TestRegistration:
    """``ProviderConfig`` → Resource Manager."""

    def test_every_enabled_provider_gets_a_queue(
        self, clock: FakeClock, sleeper: ControlledSleeper, rng: random.Random
    ) -> None:
        manager = ResourceManager(resource_config(), clock, sleeper=sleeper, rng=rng)
        _register_provider_limits(_configuration(), manager)  # type: ignore[arg-type]

        queues = {snapshot.resource: snapshot for snapshot in manager.queue_snapshots()}
        assert set(queues) == {"zero_x", "velora", "uniswap"}
        assert queues["zero_x"].requests_per_second == ZERO_X_RPS
        assert queues["velora"].requests_per_second == VELORA_RPS
        assert queues["uniswap"].requests_per_second == UNISWAP_RPS
        assert queues["uniswap"].max_concurrent == 2

    async def test_provider_interval_overrides_the_common_one(
        self, clock: FakeClock, sleeper: ControlledSleeper, rng: random.Random
    ) -> None:
        """У агрегаторов разные требования к паузе между запросами.

        Один отвечает ошибкой частоты там, где другой работает без
        замечаний, поэтому его пауза не должна задерживать остальных.
        """
        document = copy.deepcopy(level1_document())
        document["resources"] = {"provider_min_interval_seconds": 0.2}
        document["providers"] = [
            {
                "provider_id": "zero_x",
                "enabled": True,
                "api_key": {"env": "MONIK_ZEROX_API_KEY"},
                "supported_networks": ["polygon"],
                # Частота заведомо не ограничивает: проверяется пауза.
                "requests_per_second": 100.0,
                "min_interval_seconds": 0.3,
            },
            {
                "provider_id": "velora",
                "enabled": True,
                "api_key": {"env": "MONIK_VELORA_API_KEY"},
                "supported_networks": ["polygon"],
                "requests_per_second": 100.0,
            },
        ]
        environ = {**VALID_ENV, "MONIK_VELORA_API_KEY": "velora-test-key"}
        configuration = parse_configuration(document, environ=environ).config
        manager = ResourceManager(resource_config(), clock, sleeper=sleeper, rng=rng)
        _register_provider_limits(configuration, manager)

        waits: dict[ProviderId, float] = {}
        for provider in (ProviderId.ZERO_X, ProviderId.VELORA):
            before = sleeper.total_slept
            for _ in range(4):
                await manager.execute(request(provider=provider), lambda: _ok())
            waits[provider] = sleeper.total_slept - before

        # Три паузы между четырьмя запросами: 0.3 против общих 0.2.
        assert waits[ProviderId.ZERO_X] == pytest.approx(0.9, abs=0.05)
        assert waits[ProviderId.VELORA] == pytest.approx(0.6, abs=0.05)

    def test_queue_is_created_from_configuration_alone(
        self, clock: FakeClock, sleeper: ControlledSleeper, rng: random.Random
    ) -> None:
        """Новый агрегатор не требует изменений в коде очередей."""
        manager = ResourceManager(resource_config(), clock, sleeper=sleeper, rng=rng)
        manager.register_limits(
            ResourceKey(provider_id="new_aggregator"),
            ResourceLimits(max_concurrent=3, requests_per_second=7.0, burst=7),
        )
        assert [item.resource for item in manager.queue_snapshots()] == ["new_aggregator"]

    async def test_registered_limit_reaches_the_rate_limiter(
        self, clock: FakeClock, sleeper: ControlledSleeper, rng: random.Random
    ) -> None:
        """Регрессия: лимиты объявлялись, но не применялись."""
        manager = ResourceManager(resource_config(), clock, sleeper=sleeper, rng=rng)
        _register_provider_limits(_configuration(), manager)  # type: ignore[arg-type]

        for _ in range(12):
            await manager.execute(
                request(provider=ProviderId.ZERO_X, operation=CapabilityOperation.QUOTE_BUY),
                lambda: _ok(),
            )
        # Двенадцать запросов при 4.9 зап/с не помещаются в стартовый
        # запас: часть из них обязана была подождать.
        assert sleeper.total_slept > 0

    async def test_registered_burst_does_not_accumulate(
        self, clock: FakeClock, sleeper: ControlledSleeper, rng: random.Random
    ) -> None:
        """Стартовый запас — один запрос, а не floor(requests_per_second).

        Регрессия: накопленный запас выдавал за первую секунду почти
        вдвое больше настроенного, и агрегатор отвечал 429.
        """
        manager = ResourceManager(resource_config(), clock, sleeper=sleeper, rng=rng)
        _register_provider_limits(_configuration(), manager)  # type: ignore[arg-type]

        for _ in range(12):
            await manager.execute(request(provider=ProviderId.ZERO_X), lambda: _ok())

        # Один запрос проходит сразу, остальные одиннадцать — с настроенной
        # частотой. При запасе в четыре ожидание было бы на 3/4.9 меньше.
        assert sleeper.total_slept == pytest.approx(11 / ZERO_X_RPS)

    async def test_registered_queue_keeps_the_minimum_gap(
        self, clock: FakeClock, sleeper: ControlledSleeper, rng: random.Random
    ) -> None:
        """Пауза между запросами очереди берётся из конфигурации ресурсов."""
        manager = ResourceManager(resource_config(), clock, sleeper=sleeper, rng=rng)
        configuration = _configuration(provider_min_interval_seconds=0.5)
        _register_provider_limits(configuration, manager)  # type: ignore[arg-type]

        for _ in range(3):
            await manager.execute(request(provider=ProviderId.UNISWAP), lambda: _ok())

        # 0.5 секунды больше шага корзины 1/5.9, поэтому определяет она.
        assert sleeper.delays == pytest.approx([0.5, 0.5])


class TestProviderIsolation:
    async def test_one_provider_does_not_spend_another_budget(
        self, clock: FakeClock, sleeper: ControlledSleeper, rng: random.Random
    ) -> None:
        """Трафик Velora не расходует лимит Uniswap и наоборот (05 §48)."""
        manager = ResourceManager(resource_config(), clock, sleeper=sleeper, rng=rng)
        _register_provider_limits(_configuration(), manager)  # type: ignore[arg-type]

        for _ in range(20):
            await manager.execute(request(provider=ProviderId.VELORA), lambda: _ok())
        spent_on_velora = sleeper.total_slept
        assert spent_on_velora > 0

        # Uniswap ещё не отправлял запросов: его корзина полна.
        await manager.execute(request(provider=ProviderId.UNISWAP), lambda: _ok())
        assert sleeper.total_slept == spent_on_velora

    async def test_queues_are_independent_objects(
        self, clock: FakeClock, sleeper: ControlledSleeper, rng: random.Random
    ) -> None:
        manager = ResourceManager(resource_config(), clock, sleeper=sleeper, rng=rng)
        _register_provider_limits(_configuration(), manager)  # type: ignore[arg-type]
        resources = {item.resource for item in manager.queue_snapshots()}
        assert len(resources) == 3


class TestSharedBudget:
    @pytest.mark.parametrize(
        "operations",
        [
            (CapabilityOperation.QUOTE_BUY, CapabilityOperation.QUOTE_SELL),
            (CapabilityOperation.QUOTE_BUY, CapabilityOperation.FEE_DISCOVERY),
        ],
    )
    async def test_operations_share_one_provider_budget(
        self,
        clock: FakeClock,
        sleeper: ControlledSleeper,
        rng: random.Random,
        operations: tuple[CapabilityOperation, CapabilityOperation],
    ) -> None:
        """BUY и SELL одного провайдера делят общий бюджет (05 §51).

        Иначе настроенные 5.9 зап/с превратились бы в 11.8.
        """
        manager = ResourceManager(resource_config(), clock, sleeper=sleeper, rng=rng)
        _register_provider_limits(_configuration(), manager)  # type: ignore[arg-type]

        for index in range(12):
            await manager.execute(
                request(provider=ProviderId.UNISWAP, operation=operations[index % 2]),
                lambda: _ok(),
            )
        assert sleeper.total_slept > 0

    async def test_level1_and_level2_share_one_provider_budget(
        self, clock: FakeClock, sleeper: ControlledSleeper, rng: random.Random
    ) -> None:
        manager = ResourceManager(resource_config(), clock, sleeper=sleeper, rng=rng)
        _register_provider_limits(_configuration(), manager)  # type: ignore[arg-type]

        priorities = (RequestPriority.LEVEL1_BUY, RequestPriority.LEVEL2)
        for index in range(12):
            await manager.execute(
                request(provider=ProviderId.ZERO_X, priority=priorities[index % 2]),
                lambda: _ok(),
            )
        assert sleeper.total_slept > 0


class TestPriorityOrder:
    """Порядок обслуживания внутри одной очереди."""

    @staticmethod
    async def _drain(manager: ResourceManager, requests: list[tuple[str, object]]) -> list[str]:
        """Занять единственный слот и выпустить остальных по приоритету."""
        order: list[str] = []
        release = asyncio.Event()

        async def blocking() -> str:
            await release.wait()
            return "blocking"

        async def observed(label: str) -> str:
            order.append(label)
            return label

        holder = asyncio.create_task(manager.execute(request(provider=ProviderId.ZERO_X), blocking))
        await asyncio.sleep(0)

        waiting = [
            asyncio.create_task(manager.execute(item, lambda label=label: observed(label)))  # type: ignore[misc]
            for label, item in requests
        ]
        await asyncio.sleep(0)
        release.set()
        await holder
        await asyncio.gather(*waiting)
        return order

    async def test_four_priority_blocks_are_served_in_order(
        self, clock: FakeClock, sleeper: ControlledSleeper, rng: random.Random
    ) -> None:
        """Level2 ранний → Level2 поздний → Level1 SELL → Level1 BUY."""
        manager = ResourceManager(
            resource_config(global_max_concurrent_requests=1), clock, sleeper=sleeper, rng=rng
        )
        earlier = f.NOW
        later = f.NOW + timedelta(seconds=5)
        # Порядок постановки намеренно обратный ожидаемому.
        queued = [
            ("l1_buy", request(provider=ProviderId.ZERO_X, priority=RequestPriority.LEVEL1_BUY)),
            ("l1_sell", request(provider=ProviderId.ZERO_X, priority=RequestPriority.LEVEL1_SELL)),
            (
                "l2_later",
                request(
                    provider=ProviderId.ZERO_X,
                    priority=RequestPriority.LEVEL2,
                    priority_at=later,
                ),
            ),
            (
                "l2_earlier",
                request(
                    provider=ProviderId.ZERO_X,
                    priority=RequestPriority.LEVEL2,
                    priority_at=earlier,
                ),
            ),
        ]
        order = await self._drain(manager, queued)
        assert order == ["l2_earlier", "l2_later", "l1_sell", "l1_buy"]

    async def test_earlier_level2_scan_wins_even_with_a_newer_request(
        self, clock: FakeClock, sleeper: ControlledSleeper, rng: random.Random
    ) -> None:
        """Приоритет Level 2 определяется временем начала проверки (05 §17-18).

        Запрос ранней проверки создан позже — и всё равно обслуживается
        первым.
        """
        manager = ResourceManager(
            resource_config(global_max_concurrent_requests=1), clock, sleeper=sleeper, rng=rng
        )
        queued = [
            (
                "later_scan",
                request(
                    provider=ProviderId.ZERO_X,
                    priority=RequestPriority.LEVEL2,
                    priority_at=f.NOW + timedelta(seconds=10),
                    created_at=f.NOW + timedelta(seconds=10),
                ),
            ),
            (
                "earlier_scan",
                request(
                    provider=ProviderId.ZERO_X,
                    priority=RequestPriority.LEVEL2,
                    priority_at=f.NOW,
                    created_at=f.NOW + timedelta(seconds=20),
                ),
            ),
        ]
        order = await self._drain(manager, queued)
        assert order == ["earlier_scan", "later_scan"]

    async def test_fifo_inside_one_priority(
        self, clock: FakeClock, sleeper: ControlledSleeper, rng: random.Random
    ) -> None:
        """Раньше созданный запрос выполняется раньше (05 §17).

        ``asyncio.gather`` порядок не определяет, поэтому его задаёт
        очередь.
        """
        manager = ResourceManager(
            resource_config(global_max_concurrent_requests=1), clock, sleeper=sleeper, rng=rng
        )
        queued = [
            (
                "c",
                request(
                    provider=ProviderId.ZERO_X,
                    created_at=f.NOW + timedelta(seconds=3),
                    sequence=3,
                ),
            ),
            (
                "a",
                request(
                    provider=ProviderId.ZERO_X,
                    created_at=f.NOW + timedelta(seconds=1),
                    sequence=1,
                ),
            ),
            (
                "b",
                request(
                    provider=ProviderId.ZERO_X,
                    created_at=f.NOW + timedelta(seconds=2),
                    sequence=2,
                ),
            ),
        ]
        order = await self._drain(manager, queued)
        assert order == ["a", "b", "c"]


class TestBatchCost:
    async def test_batch_costs_proportionally_and_is_not_rejected(
        self, clock: FakeClock, sleeper: ControlledSleeper, rng: random.Random
    ) -> None:
        """Стоимость больше стартового запаса — это пауза, а не отказ.

        Резервация отодвигает слот пропорционально стоимости, поэтому
        ожидание конечно и предсказуемо: прежний отказ был нужен только
        циклу «подождать и попробовать снова», который мог не завершиться.
        """
        manager = ResourceManager(resource_config(), clock, sleeper=sleeper, rng=rng)
        manager.register_limits(
            ResourceKey(provider_id=ProviderId.ZERO_X),
            ResourceLimits(max_concurrent=2, requests_per_second=5.0, burst=1),
        )

        await manager.execute(request(provider=ProviderId.ZERO_X, batch_units=9), lambda: _ok())

        # Девять единиц при пяти в секунду и запасе в одну: восемь в долг.
        assert sleeper.total_slept == pytest.approx(8 / 5.0)
