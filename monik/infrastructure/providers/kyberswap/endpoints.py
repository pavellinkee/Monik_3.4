"""Endpoints и параметры KyberSwap Aggregator API.

Контракт снят с живых ответов ``aggregator-api.kyberswap.com``
2026-09-13: форма успешного ответа, набор полей ``routeSummary`` и коды
отказов проверены запросами, а не выведены из документации.

Ключ доступа API не требует. Документация предписывает передавать
``x-client-id`` с именем приложения; повышенный лимит выдаётся по
отдельному ключу в заголовке ``X-Api-Key`` и с другим базовым URL —
такой режим здесь не используется.
"""

from __future__ import annotations

from dataclasses import dataclass

from monik.domain.value_objects.identity import NetworkId

__all__ = [
    "CLIENT_ID_HEADER",
    "DEFAULT_BASE_URL",
    "DEFAULT_CLIENT_ID",
    "HEALTH_PROBES",
    "SUPPORTED_NETWORK_SLUGS",
    "HealthProbe",
    "network_slug_for",
    "routes_path",
]

#: Базовый URL публичного доступа (без ключа).
DEFAULT_BASE_URL = "https://aggregator-api.kyberswap.com"

#: Заголовок, которым приложение представляется. Ключом доступа не
#: является и на цену не влияет.
CLIENT_ID_HEADER = "x-client-id"

#: Значение заголовка, когда конфигурация своего не задала.
DEFAULT_CLIENT_ID = "monik"

#: Сети, поддержка которых заявлена адаптером, и их обозначения в пути
#: запроса. Заявлено только то, что проверено живым запросом.
SUPPORTED_NETWORK_SLUGS: dict[str, str] = {
    "polygon": "polygon",
}


@dataclass(frozen=True, slots=True)
class HealthProbe:
    """Минимальная валидная котировка для проверки доступности."""

    token_in: str
    token_out: str
    amount: str


#: Проверочные пары по сетям. Адреса — публичные канонические контракты
#: Polygon: USDT ``0xc2132D05…`` и native USDC ``0x3c499c54…``.
HEALTH_PROBES: dict[str, HealthProbe] = {
    "polygon": HealthProbe(
        token_in="0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
        token_out="0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
        amount="1000000",
    ),
}


def network_slug_for(network_id: NetworkId) -> str | None:
    """Обозначение сети в пути запроса или ``None``, если она не заявлена."""
    return SUPPORTED_NETWORK_SLUGS.get(str(network_id))


def routes_path(slug: str) -> str:
    """Путь получения маршрута для сети."""
    return f"/{slug}/api/v1/routes"
