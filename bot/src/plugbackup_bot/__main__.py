"""Запуск: `python -m plugbackup_bot` или `python -m plugbackup_bot --check`."""

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from pydantic import ValidationError

from plugbackup_bot.config import Settings, load_settings
from plugbackup_bot.db.admins import list_admins
from plugbackup_bot.domain.roles import Role
from plugbackup_bot.infra.storectl import Storectl
from plugbackup_bot.logging_setup import setup_logging
from plugbackup_bot.runtime import build_runtime, check_environment, configure_bot
from plugbackup_bot.services.health import Probe

log = logging.getLogger("plugbackup_bot")


def read_settings() -> Settings | None:
    """Настройки из .env. Ошибки печатает без значений, чтобы секреты не попали в вывод."""
    try:
        return load_settings()
    except ValidationError as error:
        print("Настройки не прошли проверку:", file=sys.stderr)
        for item in error.errors(include_input=False):
            where = ".".join(str(part) for part in item["loc"]) or "?"
            print(f"  {where}: {item['msg']}", file=sys.stderr)
        return None


async def check(settings: Settings) -> int:
    """Проверка без запуска: файлы на месте, тарифы читаются, база открывается и мигрирует."""
    problems = check_environment(settings)
    if not problems:
        from plugbackup_bot.db import Database, migrate

        db = await Database.open(settings.db_path)
        try:
            version = await migrate(db)
        finally:
            await db.close()
        print(f"Проверка пройдена. Схема базы: v{version}, нод в конфиге: {len(settings.nodes)}.")
        return 0
    print("Проверка не пройдена:", file=sys.stderr)
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    return 1


async def run(
    settings: Settings, *, storectl: Storectl | None = None, probe: Probe | None = None
) -> None:
    """Боевой запуск. storectl и probe подставляются только в devrun."""
    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    runtime = await build_runtime(settings, bot, storectl=storectl, probe=probe)
    async with runtime.db.read() as conn:
        supports = [a.tg_id for a in await list_admins(conn) if a.role is Role.SUPPORT]
    await configure_bot(bot, settings.admin_ids, supports)
    scheduler_task = asyncio.create_task(runtime.scheduler.run(), name="scheduler")
    await runtime.notifier.alert("Бот запущен.")
    try:
        # Список событий берётся из зарегистрированных handlers: в нём есть pre_checkout_query
        # и subscription, без которых оплата Stars молча не заработает.
        await runtime.dp.start_polling(bot, allowed_updates=runtime.dp.resolve_used_update_types())
    finally:
        runtime.scheduler.stop()
        await scheduler_task
        await runtime.close()
        await bot.session.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="plugbackup_bot")
    parser.add_argument("--check", action="store_true", help="проверить настройки и выйти")
    args = parser.parse_args(argv)

    settings = read_settings()
    if settings is None:
        return 2
    setup_logging(settings.log_level)
    if args.check:
        return asyncio.run(check(settings))
    log.info("starting")
    asyncio.run(run(settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
