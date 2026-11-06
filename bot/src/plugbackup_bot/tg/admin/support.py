"""Поддержка: очередь обращений, переписка, ответ клиенту из бота, закрытие."""

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from plugbackup_bot import texts_ru as T
from plugbackup_bot.domain.roles import Actor, Permission
from plugbackup_bot.emoji import E
from plugbackup_bot.services.notify import Messenger
from plugbackup_bot.services.support import (
    MAX_TEXT,
    ReopenKind,
    ReplyKind,
    SupportService,
    TicketRow,
    TicketStatus,
    TicketView,
)
from plugbackup_bot.tg.admin import core
from plugbackup_bot.tg.admin.core import (
    Adm,
    admin_nav,
    ask_screen,
    cb,
    notice_screen,
    pager,
    requires,
)
from plugbackup_bot.tg.format import fmt_datetime
from plugbackup_bot.tg.states import SupportAdminStates
from plugbackup_bot.tg.support_ui import answer_markup, answer_message, who
from plugbackup_bot.ui import buttons, cards
from plugbackup_bot.ui.markup import esc, fill
from plugbackup_bot.ui.pagination import PAGE_SIZE, Page
from plugbackup_bot.ui.screen import Screen, Ui

S = core.SUPPORT
LIST = cb(S, "list")
CLOSED_LIST = cb(S, "list", x="c")
# Переписка в карточке: влезает в сообщение (лимит 4096) вместе с шапкой.
_THREAD_CHARS = 2800


def _row_who(row: TicketRow) -> str:
    return who(row.login, row.username, row.ticket.tg_id)


def _status(status: TicketStatus) -> str:
    return T.SUPA_STATUS[status.value]


# --- экраны ---


async def list_screen(support: SupportService, number: int, *, closed: bool) -> Screen:
    x = "c" if closed else ""
    rows, total = await support.page(closed=closed, limit=PAGE_SIZE, offset=number * PAGE_SIZE)
    pages = max(1, -(-total // PAGE_SIZE))
    if number >= pages:
        number = pages - 1
        rows, total = await support.page(closed=closed, limit=PAGE_SIZE, offset=number * PAGE_SIZE)
    items = [
        [
            buttons.plain(
                fill(
                    T.SUPA_ROW,
                    id=row.ticket.id,
                    who=row.login or str(row.ticket.tg_id),
                    status=_status(row.ticket.status),
                ),
                cb(S, "card", i=row.ticket.id),
                E.support,
            )
        ]
        for row in rows
    ]
    if closed:
        body = fill(T.SUPA_SUMMARY_CLOSED, total=total) if rows else T.SUPA_EMPTY_CLOSED
        switch = buttons.plain(T.BTN_SUPA_OPENED, LIST, E.support)
    else:
        waiting = await support.waiting_count()
        body = fill(T.SUPA_SUMMARY, waiting=waiting, total=total) if rows else T.SUPA_EMPTY
        switch = buttons.plain(T.BTN_SUPA_CLOSED, CLOSED_LIST, E.ok)
    page = Page(rows, max(number, 0), pages, total)
    markup = buttons.markup([switch], *items, pager(page, S, "list", x=x), admin_nav(cb(core.MENU)))
    return Screen(cards.card(T.SUPA_TITLE, body, icon=E.support), markup)


def _thread(view: TicketView, tz: str) -> str:
    """Переписка в раскрываемой цитате. Если не влезает, остаются последние сообщения."""
    blocks = []
    for message in view.messages:
        at = fmt_datetime(message.at, tz)
        if message.author == "client":
            head = T.SUPA_MSG_CLIENT.format(at=at)
        else:
            head = T.SUPA_MSG_ADMIN.format(admin=message.admin_id, at=at)
        blocks.append(f"<b>{esc(head)}</b>\n{esc(message.text)}")
    shown: list[str] = []
    size = 0
    for block in reversed(blocks):
        if shown and size + len(block) > _THREAD_CHARS:
            break
        shown.append(block)
        size += len(block)
    shown.reverse()
    cut = len(shown) < len(blocks)
    body = "\n\n".join(shown)
    return cards.lines(f"<b>{T.SUPA_THREAD}</b>", T.SUPA_THREAD_CUT if cut else None) + (
        "\n" + cards.quote(body)
    )


def card_screen(view: TicketView, tz: str, *, flash: str | None = None) -> Screen:
    row, ticket = view.row, view.row.ticket
    facts = cards.lines(
        cards.plain_field(T.SUPA_CLIENT, _row_who(row)),
        cards.plain_field(T.SUPA_STATUS_FIELD, _status(ticket.status)),
        cards.plain_field(T.SUPA_CREATED, fmt_datetime(ticket.created_at, tz)),
        cards.plain_field(T.SUPA_UPDATED, fmt_datetime(ticket.updated_at, tz)),
    )
    text = cards.card(
        str(fill(T.SUPA_CARD_TITLE, id=ticket.id)), facts, _thread(view, tz), icon=E.support
    )
    if flash:
        text = f"{flash}\n\n{text}"
    keyboard = []
    if ticket.status is TicketStatus.CLOSED:
        keyboard.append([buttons.plain(T.BTN_SUPA_REOPEN, cb(S, "ro", i=ticket.id), E.refresh)])
    else:
        keyboard.append([buttons.primary(T.BTN_SUPA_REPLY, cb(S, "rp", i=ticket.id), E.support)])
        keyboard.append([buttons.plain(T.BTN_SUPA_CLOSE, cb(S, "cl", i=ticket.id), E.ok)])
    if row.client_id is not None:
        keyboard.append(
            [buttons.plain(T.BTN_SUPA_CLIENT, cb(core.CLIENTS, "card", i=row.client_id), E.user)]
        )
    keyboard.append([buttons.plain(T.BTN_ADM_REFRESH, cb(S, "card", i=ticket.id), E.refresh)])
    back = CLOSED_LIST if ticket.status is TicketStatus.CLOSED else LIST
    return Screen(text, buttons.markup(*keyboard, admin_nav(back)))


# --- обработчики ---


@requires(Permission.TICKETS)
async def cb_list(
    callback: CallbackQuery,
    callback_data: Adm,
    state: FSMContext,
    ui: Ui,
    support: SupportService,
    actor: Actor,
) -> None:
    await state.clear()
    await callback.answer()
    screen = await list_screen(support, callback_data.p, closed=callback_data.x == "c")
    await ui.show(callback, screen)


@requires(Permission.TICKETS)
async def cb_card(
    callback: CallbackQuery,
    callback_data: Adm,
    state: FSMContext,
    ui: Ui,
    support: SupportService,
    default_tz: str,
    actor: Actor,
) -> None:
    await state.clear()
    await callback.answer()
    view = await support.get(callback_data.i)
    if view is None:
        await ui.show(callback, notice_screen(T.ADM_NOT_FOUND, LIST))
        return
    await ui.show(callback, card_screen(view, default_tz))


@requires(Permission.TICKETS)
async def cb_reply_ask(
    callback: CallbackQuery,
    callback_data: Adm,
    state: FSMContext,
    ui: Ui,
    support: SupportService,
    default_tz: str,
    actor: Actor,
) -> None:
    await callback.answer()
    view = await support.get(callback_data.i)
    if view is None:
        await ui.show(callback, notice_screen(T.ADM_NOT_FOUND, LIST))
        return
    if view.row.ticket.status is TicketStatus.CLOSED:
        await ui.show(callback, card_screen(view, default_tz, flash=T.SUPA_ALREADY_CLOSED))
        return
    await state.clear()
    await state.update_data(ticket_id=callback_data.i)
    await state.set_state(SupportAdminStates.reply)
    body = fill(T.SUPA_ASK_REPLY, id=callback_data.i, max=MAX_TEXT)
    await ui.show(callback, ask_screen(T.SUPA_TITLE, body, cb(S, "card", i=callback_data.i)))


@requires(Permission.TICKETS)
async def msg_reply(
    message: Message,
    state: FSMContext,
    ui: Ui,
    support: SupportService,
    messenger: Messenger,
    default_tz: str,
    actor: Actor,
) -> None:
    data = await state.get_data()
    ticket_id = data.get("ticket_id")
    if not isinstance(ticket_id, int):
        await state.clear()
        await ui.show(message, notice_screen(T.ADM_STALE, LIST))
        return
    text = (message.text or "").strip()
    result = await support.reply(ticket_id, actor, text)
    if result.kind is ReplyKind.INVALID:
        body = fill(T.SUPA_BAD_REPLY, max=MAX_TEXT)
        await ui.show(message, ask_screen(T.SUPA_TITLE, body, cb(S, "card", i=ticket_id)))
        return
    await state.clear()
    if result.kind is ReplyKind.NOT_FOUND:
        await ui.show(message, notice_screen(T.ADM_NOT_FOUND, LIST))
        return
    if result.kind is ReplyKind.CLOSED or result.ticket is None:
        flash = T.SUPA_ALREADY_CLOSED
    else:
        delivered = await messenger.send(
            result.ticket.tg_id, answer_message(ticket_id, text), reply_markup=answer_markup()
        )
        flash = T.SUPA_REPLIED if delivered else T.SUPA_REPLIED_LOST
    view = await support.get(ticket_id)
    if view is None:
        await ui.show(message, notice_screen(flash, LIST))
        return
    await ui.show(message, card_screen(view, default_tz, flash=flash))


@requires(Permission.TICKETS)
async def cb_close(
    callback: CallbackQuery,
    callback_data: Adm,
    state: FSMContext,
    ui: Ui,
    support: SupportService,
    messenger: Messenger,
    default_tz: str,
    actor: Actor,
) -> None:
    await state.clear()
    await callback.answer()
    closed = await support.close(callback_data.i, actor)
    if closed is not None:
        await messenger.send(closed.tg_id, str(fill(T.SUP_CLOSED_BY_STAFF, id=closed.id)))
    view = await support.get(callback_data.i)
    if view is None:
        await ui.show(callback, notice_screen(T.ADM_NOT_FOUND, LIST))
        return
    flash = T.SUPA_CLOSED if closed is not None else T.SUPA_ALREADY_CLOSED
    await ui.show(callback, card_screen(view, default_tz, flash=flash))


@requires(Permission.TICKETS)
async def cb_reopen(
    callback: CallbackQuery,
    callback_data: Adm,
    ui: Ui,
    support: SupportService,
    default_tz: str,
    actor: Actor,
) -> None:
    await callback.answer()
    result = await support.reopen(callback_data.i, actor)
    view = await support.get(callback_data.i)
    if view is None:
        await ui.show(callback, notice_screen(T.ADM_NOT_FOUND, LIST))
        return
    flash = {
        ReopenKind.OK: T.SUPA_REOPENED,
        ReopenKind.OTHER_OPEN: T.SUPA_REOPEN_OTHER,
        ReopenKind.NOT_CLOSED: T.SUPA_REOPEN_STATE,
    }.get(result, T.ADM_NOT_FOUND)
    await ui.show(callback, card_screen(view, default_tz, flash=flash))


def register(router: Router) -> None:
    def only(*actions: str):  # type: ignore[no-untyped-def]
        return Adm.filter((F.s == S) & F.a.in_(set(actions)))

    router.callback_query.register(cb_list, only("", "list"))
    router.callback_query.register(cb_card, only("card"))
    router.callback_query.register(cb_reply_ask, only("rp"))
    router.callback_query.register(cb_close, only("cl"))
    router.callback_query.register(cb_reopen, only("ro"))
    router.message.register(
        msg_reply, StateFilter(SupportAdminStates.reply), F.text, ~F.text.startswith("/")
    )
