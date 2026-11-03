"""/restore: клиент выбирает дату снимка, бот создаёт заявку администратору."""

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from plugbackup_bot.services.notify import Messenger
from plugbackup_bot.services.restore import RestoreKind, RestoreService
from plugbackup_bot.tg import texts
from plugbackup_bot.tg.alert_texts import CB_RESTORE
from plugbackup_bot.tg.format import fmt_iso_date
from plugbackup_bot.tg.handlers.common import reply

log = logging.getLogger(__name__)

DATE = "restore:d:"
CONFIRM = "restore:ok:"
CANCEL = "restore:no"

_REFUSALS = {
    RestoreKind.NO_CLIENT: texts.RESTORE_NO_CLIENT,
    RestoreKind.BLOCKED: texts.RESTORE_BLOCKED,
    RestoreKind.BAD_DATE: texts.RESTORE_BAD_DATE,
    RestoreKind.OPEN_EXISTS: texts.RESTORE_OPEN,
    RestoreKind.TOO_MANY: texts.RESTORE_TOO_MANY,
}


def _markup(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def start(event: Message | CallbackQuery, restore: RestoreService) -> None:
    result = await restore.eligible_client(event.from_user.id)  # type: ignore[union-attr]
    if result.kind is not RestoreKind.OK:
        await reply(event, _REFUSALS[result.kind])
    else:
        rows = [
            [InlineKeyboardButton(text=fmt_iso_date(date), callback_data=f"{DATE}{date}")]
            for date in restore.available_dates()
        ]
        await reply(event, texts.RESTORE_INTRO, _markup(rows))
    if isinstance(event, CallbackQuery):
        await event.answer()


async def cb_date(callback: CallbackQuery, restore: RestoreService) -> None:
    date = (callback.data or "").removeprefix(DATE)
    await callback.answer()
    if date not in restore.available_dates():
        await reply(callback, texts.RESTORE_BAD_DATE)
        return
    rows = [
        [
            InlineKeyboardButton(text=texts.BTN_RESTORE_SEND, callback_data=f"{CONFIRM}{date}"),
            InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data=CANCEL),
        ]
    ]
    await reply(callback, texts.restore_confirm(fmt_iso_date(date)), _markup(rows))


async def cb_confirm(
    callback: CallbackQuery,
    restore: RestoreService,
    messenger: Messenger,
    admin_chat_id: int,
) -> None:
    date = (callback.data or "").removeprefix(CONFIRM)
    await callback.answer()
    result = await restore.create(callback.from_user.id, date)
    if result.kind is not RestoreKind.OK or result.request_id is None or result.client is None:
        await reply(callback, _REFUSALS[result.kind])
        return
    await reply(callback, texts.restore_accepted(result.request_id))
    client = result.client
    rows = [
        [
            InlineKeyboardButton(text="Выполнено", callback_data=f"rq:done:{result.request_id}"),
            InlineKeyboardButton(text="Отклонить", callback_data=f"rq:rej:{result.request_id}"),
        ]
    ]
    await messenger.send(
        admin_chat_id,
        f"Заявка №{result.request_id} на восстановление\n"
        f"логин: <code>{texts.esc(client.login)}</code>, tg_id: <code>{client.tg_id}</code>\n"
        f"снимок за: {texts.esc(date)}\n"
        "Автоматического отката нет: проверьте снимок на ноде и ответьте клиенту.",
        reply_markup=_markup(rows),
    )


async def cb_cancel(callback: CallbackQuery) -> None:
    await callback.answer()
    await reply(callback, texts.RESTORE_CANCELLED)


def build_router() -> Router:
    router = Router(name="restore")
    router.message.filter(F.chat.type == "private")
    router.message.register(start, Command("restore"))
    router.callback_query.register(start, F.data == CB_RESTORE)
    router.callback_query.register(cb_date, F.data.startswith(DATE))
    router.callback_query.register(cb_confirm, F.data.startswith(CONFIRM))
    router.callback_query.register(cb_cancel, F.data == CANCEL)
    return router
