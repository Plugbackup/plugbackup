"""Команды администратора. Доступны только admin_ids из конфига."""

import asyncio
import html
import logging
from collections.abc import Collection

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from plugbackup_bot.db import Database
from plugbackup_bot.domain.plans import PlanCatalog
from plugbackup_bot.infra.clock import Clock
from plugbackup_bot.services import stats as stats_service
from plugbackup_bot.services.admin import MAX_INVITES_AT_ONCE, ActionKind, AdminService
from plugbackup_bot.services.broadcast import MAX_TEXT, BroadcastService
from plugbackup_bot.services.notify import Messenger
from plugbackup_bot.services.restore import RestoreService
from plugbackup_bot.tg import admin_texts as view
from plugbackup_bot.tg import texts
from plugbackup_bot.tg.admin_filter import IsAdmin
from plugbackup_bot.tg.format import fmt_iso_date
from plugbackup_bot.tg.handlers.common import reply
from plugbackup_bot.tg.states import AdminStates

log = logging.getLogger(__name__)

_NOT_FOUND = "Клиент не найден. Укажите логин (u1001) или Telegram id."
_USAGE_INVITE = f"Использование: /invite N, где N от 1 до {MAX_INVITES_AT_ONCE}."
_USAGE_CLIENT = "Использование: /client u1001 или /client 123456789 (Telegram id)."
_USAGE_MAINT = "Использование: /maint имя_ноды текст для клиентов. Вместо имени ноды можно all."
_BROADCAST_ASK = "Пришлите текст рассылки одним сообщением. Отмена: /cancel."


async def cmd_stats(
    message: Message,
    db: Database,
    clock: Clock,
    admin: AdminService,
    catalog: PlanCatalog,
    default_tz: str,
) -> None:
    data = await stats_service.collect(db, clock, admin, default_tz, catalog.beta.id)
    await message.answer(view.stats(data, catalog, default_tz))


async def cmd_nodes(message: Message, admin: AdminService, default_tz: str) -> None:
    await message.answer(view.nodes(await admin.nodes_overview(), default_tz))


async def cmd_invite(message: Message, command: CommandObject, admin: AdminService) -> None:
    raw = (command.args or "").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= MAX_INVITES_AT_ONCE:
        await message.answer(_USAGE_INVITE)
        return
    codes = await admin.create_invites(int(raw), message.from_user.id)  # type: ignore[union-attr]
    await message.answer(view.invites(codes))


async def _show_card(
    target: Message, admin: AdminService, catalog: PlanCatalog, tz: str, client_id: int
) -> None:
    card = await admin.card(client_id)
    if card is None:
        await target.answer(_NOT_FOUND)
        return
    await target.answer(view.client_card(card, catalog, tz), reply_markup=view.client_buttons(card))


async def cmd_client(
    message: Message,
    command: CommandObject,
    admin: AdminService,
    catalog: PlanCatalog,
    default_tz: str,
) -> None:
    query = (command.args or "").strip()
    if not query:
        await message.answer(_USAGE_CLIENT)
        return
    client = await admin.find_client(query)
    if client is None:
        await message.answer(_NOT_FOUND)
        return
    await _show_card(message, admin, catalog, default_tz, client.id)


async def cb_ask(callback: CallbackQuery, admin: AdminService, catalog: PlanCatalog) -> None:
    """Шаг подтверждения перед блокировкой, возвратом или удалением."""
    _, _, action, raw_id = (callback.data or "").split(":")
    card = await admin.card(int(raw_id))
    await callback.answer()
    if card is None or action not in view.ACTION_VERBS or not isinstance(callback.message, Message):
        return
    text = f"{view.ACTION_NAMES[action]}: клиент {card.client.login}? Подтвердите."
    await callback.message.answer(
        texts.esc(text), reply_markup=view.confirm_buttons(action, card.client.id)
    )


_ACTIONS = {"s": "suspend", "r": "resume", "d": "delete"}
_NOTICES = {
    "s": view.CLIENT_SUSPENDED,
    "r": view.CLIENT_RESUMED,
    "d": view.CLIENT_DELETED,
}


async def cb_do(
    callback: CallbackQuery,
    admin: AdminService,
    messenger: Messenger,
    kick_provisioning: object,
) -> None:
    _, _, action, raw_id = (callback.data or "").split(":")
    if action not in _ACTIONS:
        await callback.answer()
        return
    result = await getattr(admin, _ACTIONS[action])(int(raw_id), callback.from_user.id)
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    if result.kind is ActionKind.OK and result.client is not None:
        await messenger.send(result.client.tg_id, _NOTICES[action])
        await callback.message.answer(
            texts.esc(f"Готово: {view.ACTION_NAMES[action].lower()}, {result.client.login}.")
        )
        if callable(kick_provisioning):
            kick_provisioning()
    elif result.kind is ActionKind.WRONG_STATE:
        await callback.message.answer(
            "Сейчас это действие недоступно: состояние клиента изменилось."
        )
    else:
        await callback.message.answer(_NOT_FOUND)


async def cb_cancel(callback: CallbackQuery) -> None:
    await callback.answer("Отменено.")


async def cb_requeue(
    callback: CallbackQuery, admin: AdminService, kick_provisioning: object
) -> None:
    raw = (callback.data or "").removeprefix("ac:rq:")
    done = raw.isdigit() and await admin.requeue_job(int(raw))
    await callback.answer("Задача возвращена в очередь." if done else "Задача не найдена.")
    if done and callable(kick_provisioning):
        kick_provisioning()


# --- рассылка ---


async def cmd_broadcast(message: Message, state: FSMContext) -> None:
    await state.set_state(AdminStates.broadcast_text)
    await message.answer(_BROADCAST_ASK)


async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.")


async def msg_broadcast_text(
    message: Message, state: FSMContext, broadcast: BroadcastService
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


async def cb_broadcast_send(
    callback: CallbackQuery,
    state: FSMContext,
    broadcast: BroadcastService,
    background_tasks: set[asyncio.Task[None]],
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


async def cb_broadcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Рассылка отменена.")


# --- плановые работы ---


async def cmd_maint(message: Message, command: CommandObject, admin: AdminService) -> None:
    target, _, text = (command.args or "").strip().partition(" ")
    if not target or not text.strip():
        await message.answer(_USAGE_MAINT)
        return
    event_id = await admin.start_maintenance(target, text)
    if event_id is None:
        await message.answer("Такой ноды нет. Укажите её имя из /nodes или all.")
        return
    await message.answer(
        f"Событие №{event_id} создано, клиенты получат сообщение. Завершить: /maint_end {event_id}"
    )


async def cmd_maint_end(message: Message, command: CommandObject, admin: AdminService) -> None:
    raw = (command.args or "").strip()
    done = raw.isdigit() and await admin.end_maintenance(int(raw))
    await message.answer("Работы отмечены завершёнными." if done else "Событие не найдено.")


# --- заявки на восстановление ---


async def cb_request(
    callback: CallbackQuery, restore: RestoreService, messenger: Messenger
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


async def cmd_admin(message: Message) -> None:
    await message.answer(view.ADMIN_HELP, reply_markup=view.admin_buttons())


async def cb_admin_stats(
    callback: CallbackQuery,
    db: Database,
    clock: Clock,
    admin: AdminService,
    catalog: PlanCatalog,
    default_tz: str,
) -> None:
    data = await stats_service.collect(db, clock, admin, default_tz, catalog.beta.id)
    await callback.answer()
    await reply(callback, view.stats(data, catalog, default_tz))


async def cb_admin_nodes(callback: CallbackQuery, admin: AdminService, default_tz: str) -> None:
    await callback.answer()
    await reply(callback, view.nodes(await admin.nodes_overview(), default_tz))


async def cb_admin_invite(callback: CallbackQuery, admin: AdminService) -> None:
    codes = await admin.create_invites(1, callback.from_user.id)
    await callback.answer()
    await reply(callback, view.invites(codes))


async def cb_admin_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.broadcast_text)
    await callback.answer()
    await reply(callback, _BROADCAST_ASK)


def build_router(admin_ids: Collection[int]) -> Router:
    router = Router(name="admin")
    only_admins = IsAdmin(admin_ids)
    router.message.filter(only_admins)
    router.callback_query.filter(only_admins)

    router.message.register(cmd_admin, Command("admin"))
    router.message.register(cmd_stats, Command("stats"))
    router.message.register(cmd_nodes, Command("nodes"))
    router.message.register(cmd_invite, Command("invite"))
    router.message.register(cmd_client, Command("client"))
    router.message.register(cmd_cancel, Command("cancel"))
    router.message.register(cmd_broadcast, Command("broadcast"))
    router.message.register(cmd_maint, Command("maint"))
    router.message.register(cmd_maint_end, Command("maint_end"))
    router.message.register(
        msg_broadcast_text, AdminStates.broadcast_text, F.text, ~F.text.startswith("/")
    )

    router.callback_query.register(cb_admin_stats, F.data == view.ADMIN_STATS)
    router.callback_query.register(cb_admin_nodes, F.data == view.ADMIN_NODES)
    router.callback_query.register(cb_admin_invite, F.data == view.ADMIN_INVITE)
    router.callback_query.register(cb_admin_broadcast, F.data == view.ADMIN_BROADCAST)
    router.callback_query.register(cb_ask, F.data.startswith("ac:ask:"))
    router.callback_query.register(cb_do, F.data.startswith("ac:do:"))
    router.callback_query.register(cb_cancel, F.data == "ac:no")
    router.callback_query.register(cb_requeue, F.data.startswith("ac:rq:"))
    router.callback_query.register(cb_broadcast_send, F.data == "bc:send")
    router.callback_query.register(cb_broadcast_cancel, F.data == "bc:cancel")
    router.callback_query.register(cb_request, F.data.startswith("rq:"))
    return router
