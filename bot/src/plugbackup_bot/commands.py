"""Меню команд бота: общее для клиентов и расширенное для админов.

Один источник и для бота (при старте), и для scripts/setup_profile.py.
"""

import logging
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeChat

log = logging.getLogger(__name__)

Menu = Sequence[tuple[str, str]]

COMMANDS: Menu = (
    ("start", "о сервисе"),
    ("status", "занято, тариф, дата списания"),
    ("upgrade", "сменить тариф"),
    ("key", "заменить SSH-ключ"),
    ("alerts", "настройки уведомлений"),
    ("restore", "запросить восстановление"),
    ("rules", "правила"),
    ("help", "справка"),
)

# Те же команды для клиентов с английским интерфейсом Telegram. Набор имён совпадает с COMMANDS.
COMMANDS_EN: Menu = (
    ("start", "about the service"),
    ("status", "usage, plan, billing date"),
    ("upgrade", "change plan"),
    ("key", "replace SSH key"),
    ("alerts", "notification settings"),
    ("restore", "request a restore"),
    ("rules", "rules"),
    ("help", "help"),
)

# Меню администратора: админские команды плюс команды клиента. Показывается только админам.
ADMIN_COMMANDS: Menu = (
    ("admin", "панель администратора"),
    ("stats", "статистика и выручка"),
    ("nodes", "ноды и их загрузка"),
    ("invite", "инвайт-коды беты"),
    ("client", "карточка клиента"),
    ("broadcast", "рассылка"),
    *COMMANDS,
)


def to_bot_commands(menu: Menu) -> list[BotCommand]:
    return [BotCommand(command=name, description=text) for name, text in menu]


@dataclass(slots=True)
class CommandsReport:
    """Что не удалось установить. Пусто в обоих полях: меню установлено везде."""

    general_failed: list[str] = field(default_factory=list)
    # Telegram не знает чат, пока админ не написал боту: меню появится после его /start.
    admins_without_chat: list[int] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.general_failed


async def set_commands(bot: Bot, admin_ids: Collection[int] = ()) -> CommandsReport:
    """Общее меню (ru по умолчанию и en) и расширенное меню каждому админу. Не бросает."""
    report = CommandsReport()
    for language, menu in ((None, COMMANDS), ("en", COMMANDS_EN)):
        try:
            await bot.set_my_commands(to_bot_commands(menu), language_code=language)
        except Exception:
            log.warning("could not set the bot commands (%s)", language or "default", exc_info=True)
            report.general_failed.append(language or "default")
    for admin_id in admin_ids:
        try:
            await bot.set_my_commands(
                to_bot_commands(ADMIN_COMMANDS), scope=BotCommandScopeChat(chat_id=admin_id)
            )
        except Exception:
            log.warning("could not set the admin commands for %s", admin_id, exc_info=True)
            report.admins_without_chat.append(admin_id)
    return report
