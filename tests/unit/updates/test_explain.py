"""Русское пояснение к обновлению."""

from __future__ import annotations

from monik.services.updates.explain import MONIK_MARK, affects_monik, describe
from monik.services.updates.ports import PendingUpdate


def _update(name: str, section: str | None = None) -> PendingUpdate:
    return PendingUpdate(name=name, available_version="1", section=section)


class TestDescription:
    def test_package_monik_runs_on_is_marked(self) -> None:
        """Оператор должен видеть, что обновление требует перезапуска."""
        text = describe(_update("libssl3t64", "libs"))

        assert MONIK_MARK in text
        assert "TLS" in text

    def test_versioned_interpreter_is_recognised(self) -> None:
        assert affects_monik(_update("python3.12", "python"))
        assert affects_monik(_update("libpython3.12-stdlib", "python"))

    def test_section_gives_category_for_unknown_package(self) -> None:
        text = describe(_update("ubuntu-release-upgrader-core", "admin"))

        assert "системное администрирование" in text
        assert MONIK_MARK not in text

    def test_unknown_section_still_gives_an_answer(self) -> None:
        """Молчание хуже общей формулировки: ответ нужен по каждой строке."""
        text = describe(_update("something-new", "universe/unheard-of"))

        assert text
        assert "сканер не затрагивает" in text

    def test_missing_section_is_not_an_error(self) -> None:
        assert describe(_update("something-new"))

    def test_unknown_package_is_not_claimed_to_affect_monik(self) -> None:
        """Эвристика ошибается в безопасную сторону."""
        assert not affects_monik(_update("motd-news-config", "admin"))
