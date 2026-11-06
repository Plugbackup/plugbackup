"""Команды плановых работ на нодах: /maint и /maint_end. Режим техработ для всего бота и
работы на одной ноде из карточки ноды живут в своих разделах."""

import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from plugbackup_bot.domain.roles import Actor, Permission
from plugbackup_bot.services.admin import AdminService
from plugbackup_bot.tg.admin.core import requires

log = logging.getLogger(__name__)

_USAGE_MAINT = "Использование: /maint имя_ноды текст для клиентов. Вместо имени ноды можно all."


# --- плановые работы ---


@requires(Permission.MAINTENANCE)
async def cmd_maint(
    message: Message, command: CommandObject, admin: AdminService, actor: Actor
) -> None:
    target, _, text = (command.args or "").strip().partition(" ")
    if not target or not text.strip():
        await message.answer(_USAGE_MAINT)
        return
    event_id = await admin.start_maintenance(target, text, actor)
    if event_id is None:
        await message.answer("Такой ноды нет. Укажите её имя из /nodes или all.")
        return
    await message.answer(
        f"Событие №{event_id} создано, клиенты получат сообщение. Завершить: /maint_end {event_id}"
    )


@requires(Permission.MAINTENANCE)
async def cmd_maint_end(
    message: Message, command: CommandObject, admin: AdminService, actor: Actor
) -> None:
    raw = (command.args or "").strip()
    done = raw.isdigit() and await admin.end_maintenance(int(raw), actor)
    await message.answer("Работы отмечены завершёнными." if done else "Событие не найдено.")


def register(router: Router) -> None:
    router.message.register(cmd_maint, Command("maint"))
    router.message.register(cmd_maint_end, Command("maint_end"))
