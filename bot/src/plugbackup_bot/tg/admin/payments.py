"""Платежи: история, поиск и возврат Stars с экраном подтверждения."""

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from plugbackup_bot import texts_ru as T
from plugbackup_bot.domain.roles import Actor, Permission
from plugbackup_bot.emoji import E
from plugbackup_bot.services.notify import Messenger
from plugbackup_bot.services.payments_admin import PaymentsAdmin, PaymentView, RefundKind
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
from plugbackup_bot.tg.format import fmt_date, fmt_datetime
from plugbackup_bot.tg.states import PaymentAdminStates
from plugbackup_bot.tg.support_ui import who
from plugbackup_bot.ui import buttons, cards
from plugbackup_bot.ui.markup import esc, fill
from plugbackup_bot.ui.pagination import PAGE_SIZE, Page
from plugbackup_bot.ui.screen import Screen, Ui

P = core.PAYMENTS
LIST = cb(P, "list")
FOUND = cb(P, "list", x="q")
MAX_QUERY = 100


def _who(view: PaymentView) -> str:
    return who(view.login, view.username, view.tg_id)


def _kind(view: PaymentView) -> str:
    if view.is_first:
        return T.PAY_KINDS["first"]
    return T.PAY_KINDS["renewal" if view.is_recurring else "single"]


# --- экраны ---


async def list_screen(payments: PaymentsAdmin, tz: str, number: int, query: str | None) -> Screen:
    x = "q" if query else ""
    rows, total = await payments.page(query, limit=PAGE_SIZE, offset=number * PAGE_SIZE)
    pages = max(1, -(-total // PAGE_SIZE))
    if number >= pages:
        number = pages - 1
        rows, total = await payments.page(query, limit=PAGE_SIZE, offset=number * PAGE_SIZE)
    items = [
        [
            buttons.plain(
                fill(
                    T.PAY_ROW,
                    id=view.id,
                    who=view.login or view.tg_id,
                    stars=view.stars,
                    date=fmt_date(view.at, tz),
                    mark=T.PAY_ROW_REFUNDED if view.status == "refunded" else "",
                ),
                cb(P, "card", i=view.id),
                E.payment,
            )
        ]
        for view in rows
    ]
    if query:
        body = fill(T.PAY_SUMMARY_FOUND, query=query, total=total) if rows else T.PAY_NOTHING
        search = buttons.plain(T.BTN_PAY_RESET, LIST, E.refresh)
    else:
        body = fill(T.PAY_SUMMARY, total=total) if rows else T.PAY_EMPTY
        search = buttons.plain(T.BTN_PAY_SEARCH, cb(P, "srch"), E.user)
    page = Page(rows, max(number, 0), pages, total)
    markup = buttons.markup([search], *items, pager(page, P, "list", x=x), admin_nav(cb(core.MENU)))
    return Screen(cards.card(T.PAY_TITLE, body, icon=E.payment), markup)


def card_screen(
    view: PaymentView, tz: str, actor: Actor, back: str, *, flash: str | None = None
) -> Screen:
    facts = [
        cards.plain_field(T.PAY_CLIENT, _who(view)),
        cards.plain_field(T.PAY_PLAN, view.plan_id),
        cards.plain_field(T.PAY_AMOUNT, fill(T.PAY_AMOUNT_VALUE, stars=view.stars)),
        cards.plain_field(T.PAY_DATE, fmt_datetime(view.at, tz)),
        cards.plain_field(T.PAY_KIND, _kind(view)),
        cards.plain_field(T.PAY_STATUS, T.PAY_STATUSES[view.status]),
        cards.field(T.PAY_CHARGE, view.charge_id),
    ]
    refund = None
    if view.status == "refunded" and view.refunded_at is not None:
        refund = fill(
            T.PAY_REFUND_INFO,
            at=fmt_datetime(view.refunded_at, tz),
            admin=view.refunded_by if view.refunded_by is not None else "—",
            access=T.PAY_ACCESS_OFF if view.refund_revoked else T.PAY_ACCESS_KEPT,
        )
    text = cards.card(
        str(fill(T.PAY_CARD_TITLE, id=view.id)), cards.lines(*facts), refund, icon=E.payment
    )
    if flash:
        text = f"{flash}\n\n{text}"
    keyboard = []
    if view.status == "paid" and actor.can(Permission.REFUNDS):
        keyboard.append([buttons.danger(T.BTN_PAY_REFUND, cb(P, "rf", i=view.id), E.warning)])
    if view.client_id is not None:
        keyboard.append(
            [buttons.plain(T.BTN_SUPA_CLIENT, cb(core.CLIENTS, "card", i=view.client_id), E.user)]
        )
    return Screen(text, buttons.markup(*keyboard, admin_nav(back)))


def refund_screen(view: PaymentView) -> Screen:
    """Экран подтверждения: красные кнопки, «Назад» отменяет."""
    body = fill(T.PAY_REFUND_ASK, stars=view.stars, who=_who(view), id=view.id)
    can_revoke = view.client_id is not None and view.is_latest
    choose = T.PAY_REFUND_CHOOSE if can_revoke else T.PAY_REFUND_OLD
    text = cards.card(T.PAY_REFUND_TITLE, body, choose, icon=E.warning)
    if can_revoke:
        rows = [
            [buttons.danger(T.BTN_PAY_REFUND_REVOKE, cb(P, "rv", i=view.id), E.warning)],
            [buttons.danger(T.BTN_PAY_REFUND_KEEP, cb(P, "rk", i=view.id), E.warning)],
        ]
    else:
        rows = [[buttons.danger(T.BTN_PAY_REFUND_ONLY, cb(P, "rk", i=view.id), E.warning)]]
    return Screen(text, buttons.markup(*rows, admin_nav(cb(P, "card", i=view.id))))


# --- обработчики ---


async def _query(state: FSMContext) -> str | None:
    value = (await state.get_data()).get("query")
    return value if isinstance(value, str) and value else None


@requires(Permission.PAYMENTS)
async def cb_list(
    callback: CallbackQuery,
    callback_data: Adm,
    state: FSMContext,
    ui: Ui,
    payments: PaymentsAdmin,
    default_tz: str,
    actor: Actor,
) -> None:
    await callback.answer()
    query = await _query(state) if callback_data.x == "q" else None
    if query is None:
        await state.clear()
    else:
        await state.set_state(None)
    await ui.show(callback, await list_screen(payments, default_tz, callback_data.p, query))


@requires(Permission.PAYMENTS)
async def cb_search_ask(callback: CallbackQuery, state: FSMContext, ui: Ui, actor: Actor) -> None:
    await callback.answer()
    await state.clear()
    await state.set_state(PaymentAdminStates.search)
    await ui.show(callback, ask_screen(T.PAY_TITLE, T.PAY_ASK_SEARCH, LIST))


@requires(Permission.PAYMENTS)
async def msg_search(
    message: Message,
    state: FSMContext,
    ui: Ui,
    payments: PaymentsAdmin,
    default_tz: str,
    actor: Actor,
) -> None:
    query = " ".join((message.text or "").split())
    if not 1 <= len(query) <= MAX_QUERY:
        body = fill(T.PAY_BAD_QUERY, max=MAX_QUERY)
        await ui.show(message, ask_screen(T.PAY_TITLE, body, LIST))
        return
    await state.set_state(None)
    await state.update_data(query=query)
    await ui.show(message, await list_screen(payments, default_tz, 0, query))


@requires(Permission.PAYMENTS)
async def cb_card(
    callback: CallbackQuery,
    callback_data: Adm,
    state: FSMContext,
    ui: Ui,
    payments: PaymentsAdmin,
    default_tz: str,
    actor: Actor,
) -> None:
    await callback.answer()
    view = await payments.get(callback_data.i)
    if view is None:
        await ui.show(callback, notice_screen(T.ADM_NOT_FOUND, LIST))
        return
    back = FOUND if await _query(state) else LIST
    await ui.show(callback, card_screen(view, default_tz, actor, back))


@requires(Permission.REFUNDS)
async def cb_refund_ask(
    callback: CallbackQuery,
    callback_data: Adm,
    ui: Ui,
    payments: PaymentsAdmin,
    actor: Actor,
) -> None:
    await callback.answer()
    view = await payments.get(callback_data.i)
    if view is None:
        await ui.show(callback, notice_screen(T.ADM_NOT_FOUND, LIST))
        return
    if view.status != "paid":
        await ui.show(callback, notice_screen(T.PAY_REFUND_ALREADY, cb(P, "card", i=view.id)))
        return
    await ui.show(callback, refund_screen(view))


async def _do_refund(
    callback: CallbackQuery,
    callback_data: Adm,
    state: FSMContext,
    ui: Ui,
    payments: PaymentsAdmin,
    messenger: Messenger,
    default_tz: str,
    actor: Actor,
    *,
    revoke: bool,
) -> None:
    await callback.answer()
    result = await payments.refund(callback_data.i, actor, revoke_access=revoke)
    back = FOUND if await _query(state) else LIST
    if result.kind is RefundKind.NOT_FOUND or result.payment is None:
        await ui.show(callback, notice_screen(T.ADM_NOT_FOUND, LIST))
        return
    view = result.payment
    if result.kind is RefundKind.ALREADY:
        flash = T.PAY_REFUND_ALREADY
    elif result.kind is RefundKind.FAILED:
        flash = str(fill(T.PAY_REFUND_FAILED, reason=result.reason or "?"))
    elif result.kind is RefundKind.OK:
        flash = str(fill(T.PAY_REFUNDED, id=view.id, stars=view.stars))
        if result.access_revoked:
            flash = f"{flash} {T.PAY_REFUNDED_REVOKED}"
        notice = T.PAY_REFUND_CLIENT.format(stars=view.stars)
        if result.access_revoked:
            notice = f"{notice}\n\n{T.PAY_REFUND_CLIENT_REVOKED}"
        await messenger.send(view.tg_id, esc(notice))
    else:
        flash = T.ADM_DENIED
    await ui.show(callback, card_screen(view, default_tz, actor, back, flash=flash))


@requires(Permission.REFUNDS)
async def cb_refund_revoke(
    callback: CallbackQuery,
    callback_data: Adm,
    state: FSMContext,
    ui: Ui,
    payments: PaymentsAdmin,
    messenger: Messenger,
    default_tz: str,
    actor: Actor,
) -> None:
    await _do_refund(
        callback, callback_data, state, ui, payments, messenger, default_tz, actor, revoke=True
    )


@requires(Permission.REFUNDS)
async def cb_refund_keep(
    callback: CallbackQuery,
    callback_data: Adm,
    state: FSMContext,
    ui: Ui,
    payments: PaymentsAdmin,
    messenger: Messenger,
    default_tz: str,
    actor: Actor,
) -> None:
    await _do_refund(
        callback, callback_data, state, ui, payments, messenger, default_tz, actor, revoke=False
    )


def register(router: Router) -> None:
    def only(*actions: str):  # type: ignore[no-untyped-def]
        return Adm.filter((F.s == P) & F.a.in_(set(actions)))

    router.callback_query.register(cb_list, only("", "list"))
    router.callback_query.register(cb_search_ask, only("srch"))
    router.callback_query.register(cb_card, only("card"))
    router.callback_query.register(cb_refund_ask, only("rf"))
    router.callback_query.register(cb_refund_revoke, only("rv"))
    router.callback_query.register(cb_refund_keep, only("rk"))
    router.message.register(
        msg_search, StateFilter(PaymentAdminStates.search), F.text, ~F.text.startswith("/")
    )
