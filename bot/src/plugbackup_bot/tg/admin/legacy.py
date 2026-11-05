"""Команды, которые ещё живут в старом виде: ноды, инвайты, рассылка, плановые работы и решения
по заявкам на восстановление. По мере переноса в новые разделы файл пустеет."""

import asyncio
import html
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from plugbackup_bot.domain.roles import Actor, Permission
from plugbackup_bot.services.admin import MAX_INVITES_AT_ONCE, AdminService
from plugbackup_bot.services.broadcast import MAX_TEXT, BroadcastService
from plugbackup_bot.services.notify import Messenger
from plugbackup_bot.services.restore import RestoreService
from plugbackup_bot.tg import admin_texts as view
from plugbackup_bot.tg import texts
from plugbackup_bot.tg.admin.core import requires
from plugbackup_bot.tg.format import fmt_iso_date
from plugbackup_bot.tg.states import AdminStates

log = logging.getLogger(__name__)

_USAGE_INVITE = f"Использование: /invite N, где N от 1 до {MAX_INVITES_AT_ONCE}."
_USAGE_MAINT = "Использование: /maint имя_ноды текст для клиентов. Вместо имени ноды можно all."
_BROADCAST_ASK = "Пришлите текст рассылки одним сообщением. Отмена: /cancel."


@requires(Permission.NODES)
async def cmd_nodes(message: Message, admin: AdminService, default_tz: str, actor: Actor) -> None:
    await message.answer(view.nodes(await admin.nodes_overview(), default_tz))


@requires(Permission.INVITES)
async def cmd_invite(
    message: Message, command: CommandObject, admin: AdminService, actor: Actor
) -> None:
    raw = (command.args or "").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= MAX_INVITES_AT_ONCE:
        await message.answer(_USAGE_INVITE)
        return
    codes = await admin.create_invites(int(raw), actor)
    await message.answer(view.invites(codes))


# --- рассылка ---


@requires(Permission.BROADCASTS)
async def cmd_broadcast(message: Message, state: FSMContext, actor: Actor) -> None:
    await state.set_state(AdminStates.broadcast_text)
    await message.answer(_BROADCAST_ASK)


@requires(Permission.BROADCASTS)
async def msg_broadcast_text(
    message: Message, state: FSMContext, broadcast: BroadcastService, actor: Actor
) -> None:
    text = (message.text or "").strip()
    if not text or len(text) > MAX_TEXT:
        await message.answer(f"Нужен текст от 1 до {MAX_TEXT} символов. Отмена: /cancel.")
        return
    await state.update_data(text=text)
    count = len(await broadcast.recipients())
    rows = [
        [
            InlineKeyboardButton(text="Отправить", callback_data="bc:send"),
            InlineKeyboardButton(text="Отмена", callback_data="bc:cancel"),
        ]
    ]
    await message.answer(
        f"Рассылка получит {count} чел. Текст:\n\n{html.escape(text)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@requires(Permission.BROADCASTS)
async def cb_broadcast_send(
    callback: CallbackQuery,
    state: FSMContext,
    broadcast: BroadcastService,
    background_tasks: set[asyncio.Task[None]],
    actor: Actor,
) -> None:
    data = await state.get_data()
    text = data.get("text")
    await state.clear()
    await callback.answer()
    if not text or not isinstance(callback.message, Message):
        return
    broadcast_id = await broadcast.create(callback.from_user.id, text)
    reply_to = callback.message

    async def deliver() -> None:
        sent, failed = await broadcast.run(broadcast_id)
        await reply_to.answer(
            f"Рассылка №{broadcast_id} завершена: доставлено {sent}, не доставлено {failed}."
        )

    task = asyncio.create_task(deliver())
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    await reply_to.answer(f"Рассылка №{broadcast_id} запущена.")


@requires(Permission.BROADCASTS)
async def cb_broadcast_cancel(callback: CallbackQuery, state: FSMContext, actor: Actor) -> None:
    await state.clear()
    await callback.answer("Рассылка отменена.")


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
    router.message.register(cmd_nodes, Command("nodes"))
    router.message.register(cmd_invite, Command("invite"))
    router.message.register(cmd_broadcast, Command("broadcast"))
    router.message.register(cmd_maint, Command("maint"))
    router.message.register(cmd_maint_end, Command("maint_end"))
    router.message.register(
        msg_broadcast_text, AdminStates.broadcast_text, F.text, ~F.text.startswith("/")
    )
    router.callback_query.register(cb_broadcast_send, F.data == "bc:send")
    router.callback_query.register(cb_broadcast_cancel, F.data == "bc:cancel")
    router.callback_query.register(cb_request, F.data.startswith("rq:"))
