"""Установка обновлений по подтверждённой команде оператора."""

from __future__ import annotations

from monik.services.updates.apt_updater import AptSystemUpdater

#: Итоговый вывод apt-get после успешной установки.
UPGRADE_OUTPUT = (
    "Reading package lists...\\n"
    "The following packages will be upgraded:\\n  base-files openssl\\n"
    "2 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\\n"
)

#: Отказ sudo, когда право не выдано.
SUDO_DENIED = "sudo: a password is required\\n"


def _updater(*, upgrade: tuple[str, ...], refresh: tuple[str, ...] = ("true",)) -> AptSystemUpdater:
    return AptSystemUpdater(refresh_command=refresh, upgrade_command=upgrade, timeout=30.0)


class TestApply:
    async def test_successful_upgrade_reports_package_count(self) -> None:
        result = await _updater(upgrade=("printf", UPGRADE_OUTPUT)).apply()

        assert result.applied
        assert result.packages == 2

    async def test_failure_is_a_result_not_an_exception(self) -> None:
        """Оператор ждёт ответа в том же диалоге, а не падения задачи."""
        result = await _updater(upgrade=("false",)).apply()

        assert not result.applied
        assert result.detail

    async def test_missing_sudo_right_is_named_explicitly(self) -> None:
        """Незавершённая настройка сервера и поломка apt — разные вещи."""
        updater = AptSystemUpdater(
            refresh_command=("true",),
            upgrade_command=("sh", "-c", f"printf '{SUDO_DENIED}'; exit 1"),
        )

        result = await updater.apply()

        assert not result.applied
        assert result.detail == "нет права выполнять установку без пароля"

    async def test_missing_command_does_not_raise(self) -> None:
        result = await _updater(upgrade=("/nonexistent/apt-get",)).apply()

        assert not result.applied

    async def test_refresh_failure_does_not_block_upgrade(self) -> None:
        """Устаревший список лучше, чем отказ ставить вообще."""
        updater = _updater(refresh=("false",), upgrade=("printf", UPGRADE_OUTPUT))

        assert (await updater.apply()).applied


class TestAvailability:
    async def test_granted_right_is_detected(self) -> None:
        updater = AptSystemUpdater(permission_command=("true",))
        assert await updater.available()

    async def test_missing_right_is_detected(self) -> None:
        updater = AptSystemUpdater(permission_command=("false",))
        assert not await updater.available()
