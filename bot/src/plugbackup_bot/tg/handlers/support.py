"""/support: клиент пишет в поддержку, ответ приходит в этот же чат."""

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from plugbackup_bot import texts_ru as T
from plugbackup_bot.emoji import E
from plugbackup_bot.services.notify import Messenger
from plugbackup_bot.services.support import MAX_TEXT, SupportService, Ticket, WriteKind
from plugbackup_bot.tg.admin import core
from plugbackup_bot.tg.admin.core import cb as admin_cb
from plugbackup_bot.tg.states import SupportStates
from plugbackup_bot.tg.support_ui import CLOSE, START, who
from plugbackup_bot.ui import buttons, cards
from plugbackup_bot.ui.buttons import markup
from plugbackup_bot.ui.markup import esc, fill
from plugbackup_bot.ui.screen import Screen, Ui

NOTICE_CHARS = 500


def intro_screen(current: Ticket | None) -> Screen:
    body = [T.SUP_INTRO]
    rows = []
    if current is not None:
        body.insert(0, str(fill(T.SUP_INTRO_OPEN, id=current.id)))
        rows.append([buttons.plain(T.BTN_SUP_CLOSE, CLOSE, E.trash)])
    text = cards.card(T.SUP_TITLE, "\n\n".join(body), icon=E.support)
    return Screen(text, markup(*rows, buttons.nav()))


async def start(
    event: Message | CallbackQuery, state: FSMContext, support: SupportService, ui: Ui
) -> None:
    await state.clear()
    await state.set_state(SupportStates.message)
    current = await support.current(event.from_user.id)  # type: ignore[union-attr]
    await ui.show(event, intro_screen(current))
    if isinstance(event, CallbackQuery):
        await event.answer()


async def msg_text(
    message: Message,
    state: FSMContext,
    support: SupportService,
    messenger: Messenger,
    admin_chat_id: int,
    ui: Ui,
) -> None:
    result = await support.write(message.chat.id, message.text or "")
    if result.kind is WriteKind.INVALID:
        text = fill(T.SUP_BAD_TEXT, max=MAX_TEXT)
        await ui.show(message, Screen(text, markup(buttons.nav())))
        return
    ticket_id = result.ticket_id or 0
    await state.clear()
    if result.kind is WriteKind.FULL:
        screen = Screen(fill(T.SUP_FULL, id=ticket_id), markup(buttons.nav()))
        await ui.show(message, screen)
        return
    template = T.SUP_ACCEPTED if result.created else T.SUP_APPENDED
    await ui.show(message, Screen(fill(template, id=ticket_id), markup(buttons.nav())))
    await _tell_admins(message, result.created, ticket_id, support, messenger, admin_chat_id)


async def _tell_admins(
    message: Message,
    created: bool,
    ticket_id: int,
    support: SupportService,
    messenger: Messenger,
    admin_chat_id: int,
) -> None:
    view = await support.get(ticket_id)
    sender = (
        who(view.row.login, view.row.username, message.chat.id) if view else str(message.chat.id)
    )
    text = (message.text or "").strip()
    shown = text if len(text) <= NOTICE_CHARS else text[:NOTICE_CHARS] + "…"
    heading = fill(T.SUPA_NEW if created else T.SUPA_MORE, id=ticket_id)
    notice = cards.lines(
        f"<b>{heading}</b>", fill(T.SUPA_NOTICE_FROM, who=sender), "", cards.quote(esc(shown))
    )
    open_button = buttons.primary(
        T.BTN_SUPA_OPEN_TICKET, admin_cb(core.SUPPORT, "card", i=ticket_id), E.support
    )
    await messenger.send(admin_chat_id, notice, reply_markup=markup([open_button]))


async def cb_close(
    callback: CallbackQuery, state: FSMContext, support: SupportService, ui: Ui
) -> None:
    await state.clear()
    closed = await support.close_own(callback.from_user.id)
    await callback.answer()
    text = fill(T.SUP_CLOSED_BY_ME, id=closed.id) if closed is not None else T.SUP_NOTHING_TO_CLOSE
    await ui.show(callback, Screen(text, markup(buttons.nav())))


def build_router() -> Router:
    router = Router(name="support")
    router.message.filter(F.chat.type == "private")
    router.message.register(start, Command("support"))
    router.callback_query.register(start, F.data == START)
    router.callback_query.register(cb_close, F.data == CLOSE)
    router.message.register(
        msg_text, StateFilter(SupportStates.message), F.text, ~F.text.startswith("/")
    )
    return router
