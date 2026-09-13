"""Адаптер KyberSwap обязан проходить общий contract suite."""

from __future__ import annotations

from monik.domain.enums.providers import ProviderId
from monik.domain.errors import ProviderError
from monik.infrastructure.http import FakeHttpClient
from monik.infrastructure.providers import AggregatorAdapter
from monik.infrastructure.providers.kyberswap import KyberSwapAdapter
from monik.services.observability import FakeClock
from tests.unit.providers.support import (
    http_returning,
    provider_config,
    resource_manager,
    secret,
)

from .adapter_contract import AdapterContractTests

#: Форма ответа снята с живого ``aggregator-api.kyberswap.com``.
ROUTES_PAYLOAD = {
    "code": 0,
    "message": "successfully",
    "data": {
        "routeSummary": {
            "tokenIn": "0xc2132d05d31c914a87c6611c10748aeb04b58e8f",
            "amountIn": "100000000",
            "amountInUsd": "99.77",
            "tokenOut": "0x53e0bca35ec356bd5dddfebbd1fc0fd03fabad39",
            "amountOut": "8899446567405885440",
            "amountOutUsd": "99.72",
            "gas": "629502",
            "gasPrice": "279394223245",
            "gasUsd": "0.0168811",
            "extraFee": {"feeAmount": "", "chargeFeeBy": "", "isInBps": False},
            "route": [[{"pool": "0xa1cf", "exchange": "uniswapv3", "poolType": "uniswapv3"}]],
            "routeID": "1cb8d1e4-a2e4-4f10-9f3a-1f2b3c4d5e6f",
            "checksum": "13253535408479079741",
        },
        "routerAddress": "0x6131B5fae19EA4f9D964eAc0408E4408b66337b5",
    },
}


class TestKyberSwapContract(AdapterContractTests):
    def make_adapter(self, clock: FakeClock) -> AggregatorAdapter:
        return KyberSwapAdapter(
            provider_config(ProviderId.KYBERSWAP),
            http=http_returning(ROUTES_PAYLOAD),
            resources=resource_manager(clock),
            clock=clock,
            api_key=secret(),
        )

    def expected_provider(self) -> ProviderId:
        return ProviderId.KYBERSWAP

    def make_failing_adapter(self, clock: FakeClock) -> AggregatorAdapter:
        return KyberSwapAdapter(
            provider_config(ProviderId.KYBERSWAP),
            http=FakeHttpClient([ProviderError("upstream unavailable")] * 5),
            resources=resource_manager(clock),
            clock=clock,
            api_key=secret(),
        )

    def make_model_breaking_adapter(self, clock: FakeClock) -> AggregatorAdapter:
        """Маршрут, чей составной идентификатор не помещается в модель."""
        summary = dict(ROUTES_PAYLOAD["data"]["routeSummary"])  # type: ignore[call-overload]
        summary["route"] = [
            [
                {"pool": f"0x{index:064x}", "exchange": f"exchange-{index:064x}"}
                for index in range(40)
            ]
        ]
        payload = {**ROUTES_PAYLOAD, "data": {"routeSummary": summary}}
        return KyberSwapAdapter(
            provider_config(ProviderId.KYBERSWAP),
            http=http_returning(payload),
            resources=resource_manager(clock),
            clock=clock,
            api_key=secret(),
        )
