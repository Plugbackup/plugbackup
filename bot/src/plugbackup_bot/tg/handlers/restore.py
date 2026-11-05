"""/restore: клиент выбирает дату снимка, бот создаёт заявку администратору."""

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from plugbackup_bot import texts_ru as T
from plugbackup_bot.emoji import E
from plugbackup_bot.services.notify import Messenger
from plugbackup_bot.services.restore import RestoreKind, RestoreService
from plugbackup_bot.tg import keyboards, texts
from plugbackup_bot.tg.alert_texts import CB_RESTORE
from plugbackup_bot.tg.format import fmt_iso_date
from plugbackup_bot.ui import buttons
from plugbackup_bot.ui.buttons import markup
from plugbackup_bot.ui.markup import fill
from plugbackup_bot.ui.screen import Screen, Ui

log = logging.getLogger(__name__)

DATE = "restore:d:"
CONFIRM = "restore:ok:"
CANCEL = "restore:no"

_REFUSALS = {
    RestoreKind.NO_CLIENT: T.RESTORE_NO_CLIENT,
    RestoreKind.BLOCKED: T.RESTORE_BLOCKED,
    RestoreKind.BAD_DATE: T.RESTORE_BAD_DATE,
    RestoreKind.OPEN_EXISTS: T.RESTORE_OPEN,
    RestoreKind.TOO_MANY: T.RESTORE_TOO_MANY,
}


def _refusal(kind: RestoreKind) -> Screen:
    return Screen(_REFUSALS[kind], markup(buttons.nav(keyboards.MENU_STATUS)))


async def start(event: Message | CallbackQuery, restore: RestoreService, ui: Ui) -> None:
    result = await restore.eligible_client(event.from_user.id)  # type: ignore[union-attr]
    if result.kind is not RestoreKind.OK:
        screen = _refusal(result.kind)
    else:
        rows = [
            [buttons.plain(fmt_iso_date(date), f"{DATE}{date}", E.refresh)]
            for date in restore.available_dates()
        ]
        screen = Screen(texts.restore_intro(), markup(*rows, buttons.nav(keyboards.MENU_STATUS)))
    await ui.show(event, screen)
    if isinstance(event, CallbackQuery):
        await event.answer()


async def cb_date(callback: CallbackQuery, restore: RestoreService, ui: Ui) -> None:
    date = (callback.data or "").removeprefix(DATE)
    await callback.answer()
    if date not in restore.available_dates():
        await ui.show(callback, Screen(T.RESTORE_BAD_DATE, markup(buttons.nav(CB_RESTORE))))
        return
    rows = [
        [
            buttons.success(T.BTN_RESTORE_SEND, f"{CONFIRM}{date}", E.ok),
            buttons.danger(T.BTN_CANCEL, CANCEL),
        ]
    ]
    await ui.show(
        callback,
        Screen(texts.restore_confirm(fmt_iso_date(date)), markup(*rows, buttons.nav(CB_RESTORE))),
    )


async def cb_confirm(
    callback: CallbackQuery,
    restore: RestoreService,
    messenger: Messenger,
    admin_chat_id: int,
    ui: Ui,
) -> None:
    date = (callback.data or "").removeprefix(CONFIRM)
    await callback.answer()
    result = await restore.create(callback.from_user.id, date)
    if result.kind is not RestoreKind.OK or result.request_id is None or result.client is None:
        await ui.show(callback, _refusal(result.kind))
        return
    await ui.show(
        callback, Screen(texts.restore_accepted(result.request_id), keyboards.only_home())
    )
    client = result.client
    rows = [
        [
            buttons.success(T.BTN_REQUEST_DONE, f"rq:done:{result.request_id}"),
            buttons.danger(T.BTN_REQUEST_REJECT, f"rq:rej:{result.request_id}"),
        ]
    ]
    await messenger.send(
        admin_chat_id,
        fill(
            T.RESTORE_ADMIN_NOTICE,
            request_id=result.request_id,
            login=client.login,
            tg_id=client.tg_id,
            date=date,
        ),
        reply_markup=markup(*rows),
    )


async def cb_cancel(callback: CallbackQuery, ui: Ui) -> None:
    await callback.answer()
    await ui.show(callback, Screen(T.RESTORE_CANCELLED, keyboards.only_home()))


def build_router() -> Router:
    router = Router(name="restore")
    router.message.filter(F.chat.type == "private")
    router.message.register(start, Command("restore"))
    router.callback_query.register(start, F.data == CB_RESTORE)
    router.callback_query.register(cb_date, F.data.startswith(DATE))
    router.callback_query.register(cb_confirm, F.data.startswith(CONFIRM))
    router.callback_query.register(cb_cancel, F.data == CANCEL)
    return router
