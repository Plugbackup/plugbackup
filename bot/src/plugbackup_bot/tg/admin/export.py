"""Экспорт CSV и копия базы. Файлы уходят владельцу в личные сообщения, не в общий чат."""

import logging
from enum import StrEnum

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from plugbackup_bot import texts_ru as T
from plugbackup_bot.domain.roles import Actor, Permission
from plugbackup_bot.emoji import E
from plugbackup_bot.services.exports import MAX_FILE_BYTES, ExportFile, ExportService
from plugbackup_bot.tg.admin import core
from plugbackup_bot.tg.admin.core import Adm, admin_nav, cb, requires
from plugbackup_bot.ui import buttons, cards
from plugbackup_bot.ui.markup import esc, fill
from plugbackup_bot.ui.screen import Screen, Ui

log = logging.getLogger(__name__)

X = core.EXPORT


class SendKind(StrEnum):
    OK = "ok"
    FAILED = "failed"
    TOO_BIG = "too_big"


def menu_screen(*, flash: str | None = None) -> Screen:
    text = cards.card(
        T.EXPORT_TITLE, T.EXPORT_INTRO, cards.quote(esc(T.EXPORT_BACKUP_NOTE)), icon=E.download
    )
    if flash:
        text = f"{flash}\n\n{text}"
    rows = [
        [buttons.plain(T.BTN_EXPORT_CLIENTS, cb(X, "cl"), E.user)],
        [buttons.plain(T.BTN_EXPORT_PAYMENTS, cb(X, "pa"), E.payment)],
        [buttons.danger(T.BTN_EXPORT_BACKUP, cb(X, "bk"), E.disk)],
    ]
    return Screen(text, buttons.markup(*rows, admin_nav(cb(core.MENU))))


async def send_file(bot: Bot, tg_id: int, file: ExportFile, caption: str) -> SendKind:
    """Отправляет файл человеку в личный чат."""
    if len(file.data) > MAX_FILE_BYTES:
        return SendKind.TOO_BIG
    try:
        await bot.send_document(
            tg_id, BufferedInputFile(file.data, filename=file.name), caption=caption
        )
    except TelegramAPIError:
        log.warning("could not send %s to %s", file.name, tg_id, exc_info=True)
        return SendKind.FAILED
    return SendKind.OK


def _outcome(kind: SendKind, ok: str) -> str:
    if kind is SendKind.OK:
        return ok
    return T.EXPORT_TOO_BIG if kind is SendKind.TOO_BIG else T.EXPORT_FAILED


async def _csv(
    callback: CallbackQuery,
    state: FSMContext,
    ui: Ui,
    bot: Bot,
    exports: ExportService,
    actor: Actor,
    what: str,
) -> None:
    await state.clear()
    await callback.answer()
    file = await (exports.clients(actor) if what == "clients" else exports.payments(actor))
    if file is None:
        await ui.show(callback, menu_screen(flash=T.ADM_DENIED))
        return
    caption = str(fill(T.EXPORT_CAPTION_CSV, title=T.EXPORT_TITLES[what], rows=file.rows))
    kind = await send_file(bot, actor.tg_id, file, caption)
    ok = str(fill(T.EXPORT_SENT, name=file.name, rows=file.rows))
    await ui.show(callback, menu_screen(flash=_outcome(kind, ok)))


@requires(Permission.EXPORT)
async def cb_clients(
    callback: CallbackQuery,
    state: FSMContext,
    ui: Ui,
    bot: Bot,
    exports: ExportService,
    actor: Actor,
) -> None:
    await _csv(callback, state, ui, bot, exports, actor, "clients")


@requires(Permission.EXPORT)
async def cb_payments(
    callback: CallbackQuery,
    state: FSMContext,
    ui: Ui,
    bot: Bot,
    exports: ExportService,
    actor: Actor,
) -> None:
    await _csv(callback, state, ui, bot, exports, actor, "payments")


@requires(Permission.EXPORT)
async def cb_menu(callback: CallbackQuery, state: FSMContext, ui: Ui, actor: Actor) -> None:
    await state.clear()
    await callback.answer()
    await ui.show(callback, menu_screen())


async def _backup(bot: Bot, exports: ExportService, actor: Actor) -> str:
    file = await exports.backup(actor)
    if file is None:
        return T.ADM_DENIED
    kind = await send_file(bot, actor.tg_id, file, T.EXPORT_CAPTION_BACKUP)
    return _outcome(kind, T.EXPORT_BACKUP_SENT)


@requires(Permission.BACKUP)
async def cb_backup(
    callback: CallbackQuery,
    state: FSMContext,
    ui: Ui,
    bot: Bot,
    exports: ExportService,
    actor: Actor,
) -> None:
    await state.clear()
    await callback.answer()
    await ui.show(callback, menu_screen(flash=await _backup(bot, exports, actor)))


@requires(Permission.BACKUP)
async def cmd_backup_db(
    message: Message, state: FSMContext, bot: Bot, exports: ExportService, actor: Actor
) -> None:
    """/backup_db: копия базы только владельцу и только в личный чат."""
    await state.clear()
    text = await _backup(bot, exports, actor)
    if message.chat.type != "private" and text == T.EXPORT_BACKUP_SENT:
        text = f"{text} {T.EXPORT_PRIVATE_NOTE}"
    await message.answer(esc(text))


def register(router: Router) -> None:
    def only(*actions: str):  # type: ignore[no-untyped-def]
        return Adm.filter((F.s == X) & F.a.in_(set(actions)))

    router.message.register(cmd_backup_db, Command("backup_db"))
    router.callback_query.register(cb_menu, only("", "list"))
    router.callback_query.register(cb_clients, only("cl"))
    router.callback_query.register(cb_payments, only("pa"))
    router.callback_query.register(cb_backup, only("bk"))
