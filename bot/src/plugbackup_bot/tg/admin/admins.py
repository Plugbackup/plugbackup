"""Администраторы: владелец добавляет и убирает помощников, у каждого свой набор прав."""

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from plugbackup_bot import texts_ru as T
from plugbackup_bot.commands import set_admin_menu
from plugbackup_bot.domain.roles import Actor, Permission, Role
from plugbackup_bot.emoji import E
from plugbackup_bot.services.admins import AddKind, AdminDirectory, RemoveKind
from plugbackup_bot.services.notify import Messenger
from plugbackup_bot.tg.admin import core
from plugbackup_bot.tg.admin.core import (
    Adm,
    admin_nav,
    ask_screen,
    cb,
    confirm_screen,
    notice_screen,
    requires,
)
from plugbackup_bot.tg.states import AdminAdminStates
from plugbackup_bot.ui import buttons, cards
from plugbackup_bot.ui.markup import esc, fill
from plugbackup_bot.ui.screen import Screen, Ui

A = core.ADMINS
LIST = cb(A, "list")
# Telegram id помещается в 52 бита; всё, что больше, точно не id.
MAX_TG_ID = 2**52


async def list_screen(directory: AdminDirectory, *, flash: str | None = None) -> Screen:
    admins = await directory.list()
    owners = [a for a in admins if a.role is Role.OWNER]
    helpers = [a for a in admins if a.role is Role.SUPPORT]
    owners_text = cards.lines(
        f"<b>{T.ADMINS_OWNERS}</b>",
        cards.bullets(f"<code>{a.tg_id}</code>" for a in owners),
        T.ADMINS_OWNERS_NOTE,
    )
    helpers_text = cards.lines(
        f"<b>{T.ADMINS_SUPPORT}</b>",
        cards.bullets(f"<code>{a.tg_id}</code>" for a in helpers)
        if helpers
        else T.ADMINS_NO_SUPPORT,
        T.ADMINS_SUPPORT_NOTE,
    )
    text = cards.card(T.ADMINS_TITLE, owners_text, helpers_text, icon=E.admin)
    if flash:
        text = f"{flash}\n\n{text}"
    rows = [[buttons.primary(T.BTN_ADMINS_ADD, cb(A, "add"), E.user)]]
    rows += [
        [buttons.danger(T.BTN_ADMINS_REMOVE.format(tg_id=a.tg_id), cb(A, "rm", i=a.tg_id), E.trash)]
        for a in helpers
    ]
    return Screen(text, buttons.markup(*rows, admin_nav(cb(core.MENU))))


@requires(Permission.ADMINS)
async def cb_list(
    callback: CallbackQuery, state: FSMContext, ui: Ui, directory: AdminDirectory, actor: Actor
) -> None:
    await state.clear()
    await callback.answer()
    await ui.show(callback, await list_screen(directory))


@requires(Permission.ADMINS)
async def cb_add_ask(callback: CallbackQuery, state: FSMContext, ui: Ui, actor: Actor) -> None:
    await callback.answer()
    await state.clear()
    await state.set_state(AdminAdminStates.add_id)
    await ui.show(callback, ask_screen(T.ADMINS_TITLE, T.ADMINS_ASK_ID, LIST))


@requires(Permission.ADMINS)
async def msg_add(
    message: Message,
    state: FSMContext,
    ui: Ui,
    bot: Bot,
    directory: AdminDirectory,
    messenger: Messenger,
    actor: Actor,
) -> None:
    raw = (message.text or "").strip()
    if not raw.isdecimal() or not 0 < int(raw) < MAX_TG_ID:
        await ui.show(message, ask_screen(T.ADMINS_TITLE, T.ADMINS_BAD_ID, LIST))
        return
    tg_id = int(raw)
    result = await directory.add_support(actor, tg_id)
    await state.clear()
    if result is AddKind.ALREADY:
        await ui.show(message, await list_screen(directory, flash=T.ADMINS_ALREADY))
        return
    if result is AddKind.DENIED:
        await ui.show(message, notice_screen(T.ADM_DENIED, LIST))
        return
    menu_set = await set_admin_menu(bot, tg_id, Role.SUPPORT)
    await messenger.send(tg_id, esc(T.ADMINS_WELCOME))
    flash = f"{fill(T.ADMINS_ADDED, tg_id=tg_id)} "
    flash += T.ADMINS_MENU_SET if menu_set else T.ADMINS_MENU_LATER
    await ui.show(message, await list_screen(directory, flash=flash))


@requires(Permission.ADMINS)
async def cb_remove_ask(
    callback: CallbackQuery, callback_data: Adm, ui: Ui, directory: AdminDirectory, actor: Actor
) -> None:
    await callback.answer()
    admins = {a.tg_id: a for a in await directory.list()}
    target = admins.get(callback_data.i)
    if target is None:
        await ui.show(callback, await list_screen(directory, flash=T.ADMINS_NOT_FOUND))
        return
    if target.role is not Role.SUPPORT or target.source != "bot":
        await ui.show(callback, await list_screen(directory, flash=T.ADMINS_PROTECTED))
        return
    body = esc(T.ADMINS_REMOVE_CONFIRM.format(tg_id=target.tg_id))
    await ui.show(callback, confirm_screen(body, yes=cb(A, "rm!", i=target.tg_id), back=LIST))


@requires(Permission.ADMINS)
async def cb_remove(
    callback: CallbackQuery,
    callback_data: Adm,
    ui: Ui,
    bot: Bot,
    directory: AdminDirectory,
    messenger: Messenger,
    actor: Actor,
) -> None:
    await callback.answer()
    tg_id = callback_data.i
    result = await directory.remove_support(actor, tg_id)
    if result is RemoveKind.OK:
        await set_admin_menu(bot, tg_id, None)
        await messenger.send(tg_id, esc(T.ADMINS_GOODBYE))
        flash = str(fill(T.ADMINS_REMOVED, tg_id=tg_id))
    elif result is RemoveKind.PROTECTED:
        flash = T.ADMINS_PROTECTED
    elif result is RemoveKind.NOT_FOUND:
        flash = T.ADMINS_NOT_FOUND
    else:
        flash = T.ADM_DENIED
    await ui.show(callback, await list_screen(directory, flash=flash))


def register(router: Router) -> None:
    def only(*actions: str):  # type: ignore[no-untyped-def]
        return Adm.filter((F.s == A) & F.a.in_(set(actions)))

    router.callback_query.register(cb_list, only("", "list"))
    router.callback_query.register(cb_add_ask, only("add"))
    router.callback_query.register(cb_remove_ask, only("rm"))
    router.callback_query.register(cb_remove, only("rm!"))
    router.message.register(
        msg_add, StateFilter(AdminAdminStates.add_id), F.text, ~F.text.startswith("/")
    )
