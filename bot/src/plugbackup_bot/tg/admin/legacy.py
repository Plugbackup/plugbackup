"""Команды, которые ещё живут в старом виде: плановые работы нод и решения по заявкам на
восстановление. По мере переноса в новые разделы файл пустеет."""

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from plugbackup_bot.domain.roles import Actor, Permission
from plugbackup_bot.services.admin import AdminService
from plugbackup_bot.services.notify import Messenger
from plugbackup_bot.services.restore import RestoreService
from plugbackup_bot.tg import texts
from plugbackup_bot.tg.admin.core import requires
from plugbackup_bot.tg.format import fmt_iso_date

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


# --- заявки на восстановление ---


@requires(Permission.RESTORE)
async def cb_request(
    callback: CallbackQuery, restore: RestoreService, messenger: Messenger, actor: Actor
) -> None:
    _, verdict, raw_id = (callback.data or "").split(":")
    if not raw_id.isdigit():
        await callback.answer()
        return
    resolved = await restore.resolve(int(raw_id), "done" if verdict == "done" else "rejected")
    if resolved is None:
        await callback.answer("Заявка уже обработана.")
        return
    await callback.answer()
    date = fmt_iso_date(resolved.snapshot_date)
    await messenger.send(
        resolved.client.tg_id,
        texts.restore_resolved(resolved.request_id, date, done=resolved.status == "done"),
    )
    if isinstance(callback.message, Message):
        status = "выполнена" if resolved.status == "done" else "отклонена"
        await callback.message.answer(f"Заявка №{resolved.request_id}: {status}, клиент уведомлён.")


def register(router: Router) -> None:
    router.message.register(cmd_maint, Command("maint"))
    router.message.register(cmd_maint_end, Command("maint_end"))
    router.callback_query.register(cb_request, F.data.startswith("rq:"))
