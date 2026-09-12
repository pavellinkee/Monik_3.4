"""Источник команд Telegram ограничен чатом оператора.

Команды останавливают production и ставят обновления системы, поэтому
вопрос «кто их прислал» относится к безопасности, а не к удобству
(``CLAUDE.md`` §35, ``19_HEALTH_MONITORING.md`` §65).
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from monik.app.container import _allowed_chat_ids
from monik.config import parse_configuration
from monik.config.loader import LoadedConfiguration
from tests.unit.config.conftest import VALID_ENV, base_document

CHAT_ID = "-1002233445566"

ENV = {
    **VALID_ENV,
    "MONIK_TELEGRAM_BOT_TOKEN": "1234567890:AA-bot-token-value",
    "MONIK_TELEGRAM_CHAT_ID": CHAT_ID,
}


def _loaded(**telegram: Any) -> LoadedConfiguration:
    document = copy.deepcopy(base_document())
    document["notifications"] = {
        "enabled": True,
        "telegram": {
            "enabled": True,
            "bot_token": {"env": "MONIK_TELEGRAM_BOT_TOKEN"},
            "chat_id": {"env": "MONIK_TELEGRAM_CHAT_ID"},
            "commands_enabled": True,
            **telegram,
        },
    }
    return parse_configuration(document, environ=ENV)


class TestAllowedChats:
    def test_operator_chat_is_taken_from_the_configured_destination(self) -> None:
        """Второй список чатов не заводится: он разошёлся бы с первым."""
        assert _allowed_chat_ids(_loaded()) == frozenset({CHAT_ID})

    def test_missing_chat_gives_an_empty_set_not_an_open_channel(self) -> None:
        """Пустой набор — сигнал не собирать канал команд вовсе."""
        document = copy.deepcopy(base_document())
        document["notifications"] = {"enabled": False}
        loaded = parse_configuration(document, environ=VALID_ENV)

        assert _allowed_chat_ids(loaded) == frozenset()

    def test_chat_id_is_not_exposed_in_the_configuration_dump(self) -> None:
        """Идентификатор чата остаётся секретом конфигурации."""
        loaded = _loaded()

        assert CHAT_ID not in loaded.config.model_dump_json()


@pytest.mark.parametrize("chat_id", ["1", "-1001234567890"])
def test_any_configured_chat_shape_is_accepted(chat_id: str) -> None:
    """Личный чат и канал выглядят по-разному, оба допустимы."""
    document = copy.deepcopy(base_document())
    document["notifications"] = {
        "enabled": True,
        "telegram": {
            "enabled": True,
            "bot_token": {"env": "MONIK_TELEGRAM_BOT_TOKEN"},
            "chat_id": {"env": "MONIK_TELEGRAM_CHAT_ID"},
        },
    }
    loaded = parse_configuration(document, environ={**ENV, "MONIK_TELEGRAM_CHAT_ID": chat_id})

    assert _allowed_chat_ids(loaded) == frozenset({chat_id})
