"""Заявки на восстановление: очередь, карточка, решение с комментарием клиенту."""

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from plugbackup_bot import texts_ru as T
from plugbackup_bot.domain.roles import Actor, Permission
from plugbackup_bot.emoji import E
from plugbackup_bot.services.notify import Messenger
from plugbackup_bot.services.restore import MAX_NOTE, RestoreService, RestoreView
from plugbackup_bot.tg import texts
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
from plugbackup_bot.tg.format import fmt_datetime, fmt_iso_date
from plugbackup_bot.tg.states import RestoreAdminStates
from plugbackup_bot.tg.support_ui import who
from plugbackup_bot.ui import buttons, cards
from plugbackup_bot.ui.markup import esc, fill
from plugbackup_bot.ui.pagination import PAGE_SIZE, Page
from plugbackup_bot.ui.screen import Screen, Ui

R = core.RESTORE
LIST = cb(R, "list")
HANDLED_LIST = cb(R, "list", x="h")
# Решения: код в callback_data, статус в базе и слово для текста.
VERDICTS = {
    "d": ("done", T.RA_VERDICT_DONE),
    "r": ("rejected", T.RA_VERDICT_REJECTED),
}


def _who(view: RestoreView) -> str:
    return who(view.client.login, view.username, view.client.tg_id)


# --- экраны ---


async def list_screen(restore: RestoreService, number: int, *, handled: bool) -> Screen:
    x = "h" if handled else ""
    rows, total = await restore.page(handled=handled, limit=PAGE_SIZE, offset=number * PAGE_SIZE)
    pages = max(1, -(-total // PAGE_SIZE))
    if number >= pages:
        number = pages - 1
        rows, total = await restore.page(
            handled=handled, limit=PAGE_SIZE, offset=number * PAGE_SIZE
        )
    items = [
        [
            buttons.plain(
                fill(
                    T.RA_ROW,
                    id=view.request_id,
                    login=view.client.login,
                    date=fmt_iso_date(view.snapshot_date),
                ),
                cb(R, "card", i=view.request_id),
                E.refresh,
            )
        ]
        for view in rows
    ]
    if handled:
        body = fill(T.RA_SUMMARY_HANDLED, total=total) if rows else T.RA_EMPTY_HANDLED
        switch = buttons.plain(T.BTN_RA_NEW, LIST, E.refresh)
    else:
        body = fill(T.RA_SUMMARY, total=total) if rows else T.RA_EMPTY
        switch = buttons.plain(T.BTN_RA_HANDLED, HANDLED_LIST, E.ok)
    page = Page(rows, max(number, 0), pages, total)
    markup = buttons.markup([switch], *items, pager(page, R, "list", x=x), admin_nav(cb(core.MENU)))
    return Screen(cards.card(T.RA_TITLE, body, icon=E.refresh), markup)


def card_screen(view: RestoreView, tz: str, *, flash: str | None = None) -> Screen:
    facts = [
        cards.plain_field(T.RA_CLIENT, _who(view)),
        cards.plain_field(T.RA_SNAPSHOT, fmt_iso_date(view.snapshot_date)),
        cards.plain_field(T.RA_CREATED, fmt_datetime(view.created_at, tz)),
        cards.plain_field(T.RA_STATUS, T.RA_STATUSES[view.status]),
    ]
    if view.handled_at is not None:
        facts.append(
            str(
                fill(
                    T.RA_HANDLED_BY,
                    admin=view.handled_by if view.handled_by is not None else "—",
                    at=fmt_datetime(view.handled_at, tz),
                )
            )
        )
    note = cards.lines(f"<b>{T.RA_NOTE}</b>", esc(view.note)) if view.note else None
    hint = T.RA_HINT if view.status == "new" else None
    text = cards.card(
        str(fill(T.RA_CARD_TITLE, id=view.request_id)),
        cards.lines(*facts),
        note,
        hint,
        icon=E.refresh,
    )
    if flash:
        text = f"{flash}\n\n{text}"
    keyboard = []
    if view.status == "new":
        keyboard.append(
            [
                buttons.success(T.BTN_RA_DONE, cb(R, "dn", i=view.request_id), E.ok),
                buttons.danger(T.BTN_RA_REJECT, cb(R, "rj", i=view.request_id), E.error),
            ]
        )
    keyboard.append(
        [buttons.plain(T.BTN_RA_CLIENT, cb(core.CLIENTS, "card", i=view.client.id), E.user)]
    )
    back = LIST if view.status == "new" else HANDLED_LIST
    return Screen(text, buttons.markup(*keyboard, admin_nav(back)))


def _note_screen(view: RestoreView, code: str) -> Screen:
    body = fill(T.RA_ASK_NOTE, id=view.request_id, verdict=VERDICTS[code][1], max=MAX_NOTE)
    text = cards.card(T.RA_TITLE, body, T.ADM_CANCEL_HINT, icon=E.refresh)
    skip = buttons.plain(T.BTN_RA_SKIP, cb(R, "sk", i=view.request_id, x=code))
    return Screen(text, buttons.markup([skip], admin_nav(cb(R, "card", i=view.request_id))))


def _confirm_screen(request_id: int, code: str, note: str | None) -> Screen:
    verdict = VERDICTS[code][1]
    body = fill(T.RA_CONFIRM, id=request_id, verdict=verdict, note=note or T.RA_NO_NOTE)
    text = cards.card(T.RA_TITLE, body, icon=E.warning)
    make = buttons.success if code == "d" else buttons.danger
    label = T.BTN_RA_DONE if code == "d" else T.BTN_RA_REJECT
    go = make(label, cb(R, "ok", i=request_id), E.ok if code == "d" else E.error)
    return Screen(text, buttons.markup([go], admin_nav(cb(R, "card", i=request_id))))


# --- обработчики ---


@requires(Permission.RESTORE)
async def cb_list(
    callback: CallbackQuery,
    callback_data: Adm,
    state: FSMContext,
    ui: Ui,
    restore: RestoreService,
    actor: Actor,
) -> None:
    await state.clear()
    await callback.answer()
    screen = await list_screen(restore, callback_data.p, handled=callback_data.x == "h")
    await ui.show(callback, screen)


@requires(Permission.RESTORE)
async def cb_card(
    callback: CallbackQuery,
    callback_data: Adm,
    state: FSMContext,
    ui: Ui,
    restore: RestoreService,
    default_tz: str,
    actor: Actor,
) -> None:
    await state.clear()
    await callback.answer()
    view = await restore.get(callback_data.i)
    if view is None:
        await ui.show(callback, notice_screen(T.ADM_NOT_FOUND, LIST))
        return
    await ui.show(callback, card_screen(view, default_tz))


@requires(Permission.RESTORE)
async def cb_old_buttons(
    callback: CallbackQuery,
    state: FSMContext,
    ui: Ui,
    restore: RestoreService,
    default_tz: str,
    actor: Actor,
) -> None:
    """Кнопки «Выполнено» и «Отклонить» из прежних уведомлений: открывают карточку заявки."""
    await state.clear()
    await callback.answer()
    raw = (callback.data or "").rsplit(":", 1)[-1]
    view = await restore.get(int(raw)) if raw.isdecimal() else None
    if view is None:
        await ui.show(callback, notice_screen(T.ADM_NOT_FOUND, LIST))
        return
    await ui.show(callback, card_screen(view, default_tz))


async def _ask_note(
    callback: CallbackQuery,
    callback_data: Adm,
    state: FSMContext,
    ui: Ui,
    restore: RestoreService,
    default_tz: str,
    code: str,
) -> None:
    await callback.answer()
    view = await restore.get(callback_data.i)
    if view is None:
        await ui.show(callback, notice_screen(T.ADM_NOT_FOUND, LIST))
        return
    if view.status != "new":
        await ui.show(callback, card_screen(view, default_tz, flash=T.RA_TOO_LATE))
        return
    await state.clear()
    await state.update_data(request_id=view.request_id, verdict=code)
    await state.set_state(RestoreAdminStates.note)
    await ui.show(callback, _note_screen(view, code))


@requires(Permission.RESTORE)
async def cb_done(
    callback: CallbackQuery,
    callback_data: Adm,
    state: FSMContext,
    ui: Ui,
    restore: RestoreService,
    default_tz: str,
    actor: Actor,
) -> None:
    await _ask_note(callback, callback_data, state, ui, restore, default_tz, "d")


@requires(Permission.RESTORE)
async def cb_reject(
    callback: CallbackQuery,
    callback_data: Adm,
    state: FSMContext,
    ui: Ui,
    restore: RestoreService,
    default_tz: str,
    actor: Actor,
) -> None:
    await _ask_note(callback, callback_data, state, ui, restore, default_tz, "r")


@requires(Permission.RESTORE)
async def msg_note(message: Message, state: FSMContext, ui: Ui, actor: Actor) -> None:
    data = await state.get_data()
    request_id, code = data.get("request_id"), data.get("verdict")
    if not isinstance(request_id, int) or code not in VERDICTS:
        await state.clear()
        await ui.show(message, notice_screen(T.RA_LOST, LIST))
        return
    note = (message.text or "").strip()
    if not 1 <= len(note) <= MAX_NOTE:
        body = fill(T.RA_BAD_NOTE, max=MAX_NOTE)
        await ui.show(message, ask_screen(T.RA_TITLE, body, cb(R, "card", i=request_id)))
        return
    await state.update_data(note=note)
    await state.set_state(None)
    await ui.show(message, _confirm_screen(request_id, str(code), note))


@requires(Permission.RESTORE)
async def cb_skip_note(
    callback: CallbackQuery, callback_data: Adm, state: FSMContext, ui: Ui, actor: Actor
) -> None:
    await callback.answer()
    data = await state.get_data()
    if data.get("request_id") != callback_data.i or data.get("verdict") not in VERDICTS:
        await state.clear()
        await ui.show(callback, notice_screen(T.RA_LOST, LIST))
        return
    await state.update_data(note=None)
    await state.set_state(None)
    await ui.show(callback, _confirm_screen(callback_data.i, str(data["verdict"]), None))


@requires(Permission.RESTORE)
async def cb_confirm(
    callback: CallbackQuery,
    callback_data: Adm,
    state: FSMContext,
    ui: Ui,
    restore: RestoreService,
    messenger: Messenger,
    default_tz: str,
    actor: Actor,
) -> None:
    await callback.answer()
    data = await state.get_data()
    code = data.get("verdict")
    if data.get("request_id") != callback_data.i or code not in VERDICTS:
        await state.clear()
        await ui.show(callback, notice_screen(T.RA_LOST, LIST))
        return
    await state.clear()
    status, word = VERDICTS[str(code)]
    note = data.get("note")
    resolved = await restore.resolve(
        callback_data.i, status, note if isinstance(note, str) else None, actor
    )
    view = await restore.get(callback_data.i)
    if resolved is None:
        if view is None:
            await ui.show(callback, notice_screen(T.ADM_NOT_FOUND, LIST))
        else:
            await ui.show(callback, card_screen(view, default_tz, flash=T.RA_TOO_LATE))
        return
    await messenger.send(
        resolved.client.tg_id,
        texts.restore_resolved(
            resolved.request_id,
            fmt_iso_date(resolved.snapshot_date),
            done=status == "done",
            note=resolved.note,
        ),
    )
    flash = str(fill(T.RA_DONE_FLASH, id=resolved.request_id, verdict=word))
    if view is None:
        await ui.show(callback, notice_screen(flash, LIST))
        return
    await ui.show(callback, card_screen(view, default_tz, flash=flash))


def register(router: Router) -> None:
    def only(*actions: str):  # type: ignore[no-untyped-def]
        return Adm.filter((F.s == R) & F.a.in_(set(actions)))

    router.callback_query.register(cb_list, only("", "list"))
    router.callback_query.register(cb_card, only("card"))
    router.callback_query.register(cb_done, only("dn"))
    router.callback_query.register(cb_reject, only("rj"))
    router.callback_query.register(cb_skip_note, only("sk"))
    router.callback_query.register(cb_confirm, only("ok"))
    router.callback_query.register(cb_old_buttons, F.data.startswith("rq:"))
    router.message.register(
        msg_note, StateFilter(RestoreAdminStates.note), F.text, ~F.text.startswith("/")
    )
