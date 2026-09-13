"""Пояснение к неподтверждённой сумме на русском языке.

Level 2 формулирует причину по-английски и одинаково для всех
провайдеров. Оператор читает уведомление по-русски, поэтому известные
формулировки переводятся здесь — в одном месте и заранее.

Незнакомая формулировка **не переводится и не выбрасывается**: она
показывается как есть. Придумывать перевод для причины, которой мы не
видели, значит подменять диагностику догадкой.
"""

from __future__ import annotations

__all__ = ["describe_reason"]

#: Известные причины и их русские формулировки. Ключ — начало строки,
#: потому что часть причин дополняется подробностями провайдера.
_REASONS: tuple[tuple[str, str], ...] = (
    (
        "net result is below the profitability threshold",
        "итог ниже порога прибыльности",
    ),
    (
        "opportunity expired before level 2 verification",
        "возможность истекла до начала проверки",
    ),
    ("calculation is partial", "расчёт неполон: часть расходов неизвестна"),
    ("calculation is invalid", "расчёт невозможен: данные противоречивы"),
    ("calculation is unknown", "расчёт не выполнен: значение неизвестно"),
    ("buy route not confirmed", "маршрут покупки не воспроизведён"),
    ("sell route not confirmed", "маршрут продажи не воспроизведён"),
)


def describe_reason(reason: str | None) -> str | None:
    """Русское пояснение причины или исходный текст, если он незнаком."""
    if reason is None:
        return None
    lowered = reason.lower()
    for prefix, translation in _REASONS:
        if lowered.startswith(prefix):
            return translation
    return reason
