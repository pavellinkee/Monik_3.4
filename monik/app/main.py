"""Application entrypoint.

Здесь нет business logic (``25_PROJECT_STRUCTURE.md`` §5): модуль загружает
конфигурацию, собирает приложение и передаёт управление его жизненному
циклу.

Порядок запуска соответствует ``CLAUDE.md`` §30.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
from collections.abc import Sequence

from monik import APPLICATION_VERSION, version_label
from monik.app.control import RESTART_EXIT_CODE
from monik.app.lifecycle import Application, create_application
from monik.config import configuration_diagnostics, load_configuration
from monik.config.environment import EnvFileResult, load_env_file
from monik.config.sections.application import Environment
from monik.domain.enums.health import SupervisorState
from monik.domain.errors import MonikError
from monik.services.observability import configure_logging, secret_registry
from monik.services.observability.clock import SystemClock
from monik.services.observability.logging import get_logger, log_fields

__all__ = ["main", "run"]

_LOGGER = get_logger("app.main")

#: Код возврата при аварийной остановке.
_EXIT_SAFE_STOP = 2

#: Код возврата при ошибке конфигурации или запуска.
_EXIT_ERROR = 1


def build_parser() -> argparse.ArgumentParser:
    """Аргументы командной строки."""
    parser = argparse.ArgumentParser(prog="monik", description="Monik DEX arbitrage scanner")
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="path to the configuration file",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help=(
            "path to the file with secrets; by default the first existing of "
            "$MONIK_ENV_FILE, /etc/monik/monik.env, ./.env is used"
        ),
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="validate the configuration and exit without starting workers",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=version_label(),
        help="print the application version and exit",
    )
    return parser


async def _run(
    config_path: str,
    *,
    check_only: bool,
    environment_file: EnvFileResult | None = None,
) -> int:
    """Загрузить конфигурацию и выполнить жизненный цикл приложения."""
    loaded = load_configuration(config_path, registry=secret_registry)
    configure_logging(
        level=loaded.config.logging.level.value,
        registry=secret_registry,
        timezone=loaded.config.application.timezone,
    )
    environment = loaded.config.application.environment
    if environment_file is not None and environment_file.found:
        # Путь и число имён — не секреты; значения не логируются.
        _LOGGER.info(
            "environment file loaded",
            extra=log_fields(
                path=str(environment_file.path),
                variables=environment_file.loaded,
                already_set=environment_file.skipped,
            ),
        )
    _LOGGER.info(
        "starting %s",
        version_label(),
        extra=log_fields(
            application_version=APPLICATION_VERSION,
            environment=environment.value,
        ),
    )
    if environment is Environment.DEVELOPMENT:
        # Значение по умолчанию. Боевой запуск обязан задать окружение
        # явно, иначе диагностика и уведомления называют production
        # development (``17_CONFIGURATION.md`` §13).
        _LOGGER.warning(
            "application environment is 'development' (the default); "
            "set application.environment or MONIK__APPLICATION__ENVIRONMENT "
            "to 'production' on a production deployment",
            extra=log_fields(environment=environment.value),
        )
    _LOGGER.info("configuration loaded", extra=log_fields(**configuration_diagnostics(loaded)))
    if check_only:
        return 0

    application, database = await create_application(loaded, clock=SystemClock())
    try:
        await application.startup()
        _install_signal_handlers(application)
        state = await application.run()
        restart_requested = application.container.control.restart_requested
    finally:
        await application.shutdown()
        await database.close()

    if state is SupervisorState.SAFE_STOP:
        _LOGGER.error("application stopped in SAFE_STOP")
        return _EXIT_SAFE_STOP
    if restart_requested:
        # Отдельный код возврата отличает запрошенный перезапуск от
        # обычной остановки. Поднимает процесс менеджер служб.
        _LOGGER.warning("application stopped for a requested restart")
        return RESTART_EXIT_CODE
    return 0


def _install_signal_handlers(application: Application) -> None:
    """Остановка по SIGINT/SIGTERM выполняется graceful (``14`` §49)."""
    loop = asyncio.get_running_loop()
    for signal_name in ("SIGINT", "SIGTERM"):
        handled = getattr(signal, signal_name, None)
        if handled is None:  # pragma: no cover - платформа без сигнала
            continue
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(handled, application.request_stop)


def main(argv: Sequence[str] | None = None) -> int:
    """Запустить приложение и вернуть код возврата."""
    arguments = build_parser().parse_args(argv)
    # Секреты подтягиваются до загрузки конфигурации: ссылки { env: ... }
    # разрешаются уже по готовому окружению. Файл живёт вне репозитория,
    # поэтому копия на другом сервере находит его сама.
    environment_file = load_env_file(arguments.env_file)
    try:
        return asyncio.run(
            _run(
                arguments.config,
                check_only=arguments.check_config,
                environment_file=environment_file,
            )
        )
    except MonikError as error:
        # Ошибка нормализована: наружу не выходит трассировка библиотеки.
        sys.stderr.write(f"monik: {error.info.code}: {error.info.message}\n")
        return _EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover - интерактивное прерывание
        return 0


def run() -> int:
    """Console entrypoint ``monik``."""
    return main()


if __name__ == "__main__":  # pragma: no cover - тонкая обёртка
    raise SystemExit(run())
