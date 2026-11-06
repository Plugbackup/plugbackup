"""Рассылки: сегмент, текст, кнопки-ссылки, время, проверка и отправка; список и статистика."""

from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from plugbackup_bot import texts_ru as T
from plugbackup_bot.domain.roles import Actor, Permission
from plugbackup_bot.emoji import E
from plugbackup_bot.infra.clock import Clock
from plugbackup_bot.services.broadcast import (
    MAX_BUTTONS,
    MAX_SCHEDULE_DAYS,
    MAX_TEXT,
    Broadcast,
    BroadcastService,
    BroadcastStatus,
    ButtonProblem,
    Segment,
    parse_buttons,
    parse_send_time,
)
from plugbackup_bot.tg.admin import core
from plugbackup_bot.tg.admin.core import (
    Adm,
    admin_nav,
    ask_screen,
    cb,
    confirm_screen,
    notice_screen,
    pager,
    requires,
)
from plugbackup_bot.tg.format import fmt_datetime
from plugbackup_bot.tg.states import BroadcastStates
from plugbackup_bot.ui import buttons, cards
from plugbackup_bot.ui.markup import esc, fill
from plugbackup_bot.ui.pagination import PAGE_SIZE, Page
from plugbackup_bot.ui.screen import Screen, Ui

B = core.BROADCASTS
LIST = cb(B, "list")
# Предпросмотр показывает текст целиком, но в сообщении лимит 4096: длинный хвост сворачиваем.
_PREVIEW_CHARS = 1500


def segment_name(segment: str) -> str:
    if segment in T.BC_SEGMENT_NAMES:
        return T.BC_SEGMENT_NAMES[segment]
    if segment.startswith("node:"):
        return T.BC_NODE_SEGMENT.format(id=segment.removeprefix("node:"))
    return segment


def _when(ts: int | None, tz: str) -> str:
    return fmt_datetime(ts, tz) if ts else "—"


def _state_label(item: Broadcast, tz: str) -> str:
    template = T.BC_STATE[item.status.value]
    return template.format(
        when=_when(item.send_at, tz),
        done=item.sent + item.blocked + item.failed,
        total=item.total,
        sent=item.sent,
    )


# --- экраны ---


async def list_screen(service: BroadcastService, tz: str, number: int) -> Screen:
    rows, total = await service.page(limit=PAGE_SIZE, offset=number * PAGE_SIZE)
    pages = max(1, -(-total // PAGE_SIZE))
    if number >= pages:
        number = pages - 1
        rows, total = await service.page(limit=PAGE_SIZE, offset=number * PAGE_SIZE)
    items = [
        [
            buttons.plain(
                fill(
                    T.BC_ROW,
                    id=item.id,
                    segment=segment_name(item.segment),
                    state=_state_label(item, tz),
                ),
                cb(B, "card", i=item.id),
                E.bell,
            )
        ]
        for item in rows
    ]
    body = fill(T.BC_SUMMARY, total=total, open=await service.open_count()) if rows else T.BC_EMPTY
    page = Page(rows, max(number, 0), pages, total)
    markup = buttons.markup(
        [buttons.primary(T.BTN_BC_NEW, cb(B, "new"), E.bell)],
        *items,
        pager(page, B, "list"),
        admin_nav(cb(core.MENU)),
    )
    return Screen(cards.card(T.BC_TITLE, body, icon=E.bell), markup)


async def segment_screen(service: BroadcastService) -> Screen:
    counts = await service.segment_counts()
    rows = [
        [
            buttons.plain(
                T.BTN_BC_SEGMENT.format(
                    name=T.BC_SEGMENT_NAMES[seg.value], count=counts[seg.value]
                ),
                cb(B, "seg", x=seg.value),
            )
        ]
        for seg in Segment
    ]
    hints = cards.bullets(
        f"<b>{esc(T.BC_SEGMENT_NAMES[seg.value])}</b>: {esc(T.BC_SEGMENT_HINTS[seg.value])}"
        for seg in Segment
    )
    text = cards.card(T.BC_NEW_TITLE, T.BC_ASK_SEGMENT, hints, icon=E.bell)
    return Screen(text, buttons.markup(*rows, admin_nav(LIST)))


def _buttons_lines(items: tuple[tuple[str, str], ...]) -> str:
    return cards.bullets(f"{esc(label)} → {esc(url)}" for label, url in items)


def preview_screen(
    *, segment: str, count: int, text: str, items: tuple[tuple[str, str], ...],
    send_at: int | None, tz: str,
) -> Screen:  # fmt: skip
    """Экран проверки: он же подтверждение. Красная кнопка отправляет рассылку."""
    shown = text if len(text) <= _PREVIEW_CHARS else text[:_PREVIEW_CHARS] + "…"
    facts = cards.lines(
        cards.plain_field(T.BC_PREVIEW_TO, segment_name(segment)),
        cards.plain_field(T.BC_PREVIEW_COUNT, count),
        cards.plain_field(T.BC_PREVIEW_WHEN, _when(send_at, tz) if send_at else T.BC_PREVIEW_NOW),
    )
    extra = cards.lines(f"<b>{T.BC_PREVIEW_BUTTONS}</b>", _buttons_lines(items)) if items else None
    note = T.BC_PREVIEW_NOBODY if count == 0 else T.BC_PREVIEW_NOTE
    body = cards.card(
        T.BC_PREVIEW_TITLE, facts, cards.quote(esc(shown)), extra, note, icon=E.warning
    )
    go = T.BTN_BC_SCHEDULE if send_at else T.BTN_BC_SEND
    rows = [[buttons.plain(T.BTN_BC_TEST, cb(B, "test"), E.bell)]]
    if count:
        rows.append([buttons.danger(go, cb(B, "go"), E.warning)])
    return Screen(body, buttons.markup(*rows, admin_nav(LIST)))


def card_screen(item: Broadcast, tz: str, *, flash: str | None = None) -> Screen:
    facts = [
        cards.plain_field(T.BC_CARD_SEGMENT, segment_name(item.segment)),
        cards.plain_field(T.BC_CARD_STATE, T.BC_STATUS_NAMES[item.status.value]),
    ]
    if item.kind == "maintenance":
        facts.append(T.BC_KIND_MAINTENANCE)
    facts.append(cards.plain_field(T.BC_CARD_CREATED, _when(item.created_at, tz)))
    if item.status is BroadcastStatus.SCHEDULED:
        facts.append(cards.plain_field(T.BC_CARD_PLANNED, _when(item.send_at, tz)))
    if item.started_at:
        facts.append(cards.plain_field(T.BC_CARD_STARTED, _when(item.started_at, tz)))
    if item.finished_at:
        facts.append(cards.plain_field(T.BC_CARD_FINISHED, _when(item.finished_at, tz)))
    stats = None
    if item.total or item.sent:
        lines = [
            fill(T.BC_CARD_TOTAL, total=item.total or item.sent + item.failed),
            fill(T.BC_CARD_SENT, sent=item.sent),
            fill(T.BC_CARD_BLOCKED, blocked=item.blocked),
            fill(T.BC_CARD_FAILED, failed=item.failed),
        ]
        if item.status in (BroadcastStatus.SENDING, BroadcastStatus.CANCELLED) and item.pending:
            lines.append(fill(T.BC_CARD_PENDING, pending=item.pending))
        stats = cards.lines(f"<b>{T.BC_CARD_STATS}</b>", cards.bullets(lines))
    shown = item.text if len(item.text) <= _PREVIEW_CHARS else item.text[:_PREVIEW_CHARS] + "…"
    extra = (
        cards.lines(f"<b>{T.BC_CARD_BUTTONS}</b>", _buttons_lines(item.buttons))
        if (item.buttons)
        else None
    )
    text = cards.card(
        fill(T.BC_CARD_TITLE, id=item.id),
        cards.lines(*facts),
        stats,
        cards.lines(f"<b>{T.BC_CARD_TEXT}</b>", cards.quote(esc(shown))),
        extra,
        icon=E.bell,
    )
    if flash:
        text = f"{flash}\n\n{text}"
    rows = [[buttons.plain(T.BTN_ADM_REFRESH, cb(B, "card", i=item.id), E.refresh)]]
    if item.status in (BroadcastStatus.SCHEDULED, BroadcastStatus.SENDING):
        rows.append([buttons.danger(T.BTN_BC_CANCEL, cb(B, "cx", i=item.id), E.trash)])
    return Screen(text, buttons.markup(*rows, admin_nav(LIST)))


# --- черновик в FSM ---


def _draft_buttons(data: dict[str, object]) -> tuple[tuple[str, str], ...]:
    raw = data.get("buttons") or []
    return tuple((str(label), str(url)) for label, url in raw)  # type: ignore[attr-defined]


async def _show_preview(
    event: Message | CallbackQuery,
    state: FSMContext,
    ui: Ui,
    service: BroadcastService,
    tz: str,
) -> None:
    data = await state.get_data()
    segment, text = data.get("segment"), data.get("text")
    if not isinstance(segment, str) or not isinstance(text, str):
        await state.clear()
        await ui.show(event, notice_screen(T.BC_LOST, LIST))
        return
    await state.set_state(None)
    count = len(await service.recipients(segment))
    send_at = data.get("send_at")
    screen = preview_screen(
        segment=segment,
        count=count,
        text=text,
        items=_draft_buttons(data),
        send_at=int(send_at) if isinstance(send_at, int) else None,
        tz=tz,
    )
    await ui.show(event, screen)


# --- обработчики: список и карточка ---


@requires(Permission.BROADCASTS)
async def cb_list(
    callback: CallbackQuery,
    callback_data: Adm,
    state: FSMContext,
    ui: Ui,
    broadcast: BroadcastService,
    default_tz: str,
    actor: Actor,
) -> None:
    await state.clear()
    await callback.answer()
    await ui.show(callback, await list_screen(broadcast, default_tz, callback_data.p))


@requires(Permission.BROADCASTS)
async def cmd_broadcast(
    message: Message,
    state: FSMContext,
    ui: Ui,
    broadcast: BroadcastService,
    default_tz: str,
    actor: Actor,
) -> None:
    await state.clear()
    await ui.show(message, await list_screen(broadcast, default_tz, 0))


@requires(Permission.BROADCASTS)
async def cb_card(
    callback: CallbackQuery,
    callback_data: Adm,
    ui: Ui,
    broadcast: BroadcastService,
    default_tz: str,
    actor: Actor,
) -> None:
    await callback.answer()
    item = await broadcast.get(callback_data.i)
    if item is None:
        await ui.show(callback, notice_screen(T.ADM_NOT_FOUND, LIST))
        return
    await ui.show(callback, card_screen(item, default_tz))


# --- обработчики: создание ---


@requires(Permission.BROADCASTS)
async def cb_new(
    callback: CallbackQuery,
    state: FSMContext,
    ui: Ui,
    broadcast: BroadcastService,
    actor: Actor,
) -> None:
    await state.clear()
    await callback.answer()
    await ui.show(callback, await segment_screen(broadcast))


@requires(Permission.BROADCASTS)
async def cb_segment(
    callback: CallbackQuery,
    callback_data: Adm,
    state: FSMContext,
    ui: Ui,
    broadcast: BroadcastService,
    actor: Actor,
) -> None:
    await callback.answer()
    segment = callback_data.x
    if segment not in {seg.value for seg in Segment}:
        await ui.show(callback, notice_screen(T.ADM_STALE, LIST))
        return
    count = len(await broadcast.recipients(segment))
    await state.clear()
    await state.update_data(segment=segment)
    await state.set_state(BroadcastStates.text)
    body = fill(T.BC_ASK_TEXT, segment=segment_name(segment), count=count, max=MAX_TEXT)
    await ui.show(callback, ask_screen(T.BC_NEW_TITLE, body, cb(B, "new")))


@requires(Permission.BROADCASTS)
async def msg_text(message: Message, state: FSMContext, ui: Ui, actor: Actor) -> None:
    text = (message.text or "").strip()
    if not 1 <= len(text) <= MAX_TEXT:
        hint = T.BC_BAD_TEXT.format(max=MAX_TEXT)
        await ui.show(message, ask_screen(T.BC_NEW_TITLE, hint, cb(B, "new")))
        return
    await state.update_data(text=text)
    await state.set_state(BroadcastStates.buttons)
    await ui.show(message, _buttons_ask())


def _buttons_ask() -> Screen:
    body = fill(T.BC_ASK_BUTTONS, max=MAX_BUTTONS)
    text = cards.card(T.BC_NEW_TITLE, body, T.ADM_CANCEL_HINT, icon=E.bell)
    rows = [[buttons.plain(T.BTN_BC_NO_BUTTONS, cb(B, "nb"))]]
    return Screen(text, buttons.markup(*rows, admin_nav(cb(B, "new"))))


@requires(Permission.BROADCASTS)
async def msg_buttons(
    message: Message,
    state: FSMContext,
    ui: Ui,
    actor: Actor,
) -> None:
    try:
        items = parse_buttons(message.text or "")
    except ButtonProblem as error:
        screen = ask_screen(T.BC_NEW_TITLE, T.ADM_TYPE_ERROR.format(hint=error), cb(B, "new"))
        await ui.show(message, screen)
        return
    await state.update_data(buttons=[list(item) for item in items])
    await state.set_state(None)
    await ui.show(message, _when_ask())


def _when_ask() -> Screen:
    rows = [
        [buttons.plain(T.BTN_BC_NOW, cb(B, "now"))],
        [buttons.plain(T.BTN_BC_LATER, cb(B, "later"))],
    ]
    text = cards.card(T.BC_NEW_TITLE, T.BC_ASK_WHEN, icon=E.clock)
    return Screen(text, buttons.markup(*rows, admin_nav(cb(B, "new"))))


@requires(Permission.BROADCASTS)
async def cb_no_buttons(callback: CallbackQuery, state: FSMContext, ui: Ui, actor: Actor) -> None:
    await callback.answer()
    if "text" not in await state.get_data():
        await ui.show(callback, notice_screen(T.BC_LOST, LIST))
        return
    await state.update_data(buttons=[])
    await state.set_state(None)
    await ui.show(callback, _when_ask())


@requires(Permission.BROADCASTS)
async def cb_now(
    callback: CallbackQuery,
    state: FSMContext,
    ui: Ui,
    broadcast: BroadcastService,
    default_tz: str,
    actor: Actor,
) -> None:
    await callback.answer()
    await state.update_data(send_at=None)
    await _show_preview(callback, state, ui, broadcast, default_tz)


@requires(Permission.BROADCASTS)
async def cb_later(
    callback: CallbackQuery,
    state: FSMContext,
    ui: Ui,
    clock: Clock,
    default_tz: str,
    actor: Actor,
) -> None:
    await callback.answer()
    if "text" not in await state.get_data():
        await ui.show(callback, notice_screen(T.BC_LOST, LIST))
        return
    await state.set_state(BroadcastStates.when)
    example = fmt_example(clock.now() + 2 * 86400, default_tz)
    body = fill(T.BC_ASK_DATETIME, tz=default_tz, example=example, days=MAX_SCHEDULE_DAYS)
    await ui.show(callback, ask_screen(T.BC_NEW_TITLE, body, cb(B, "new")))


def fmt_example(ts: int, tz: str) -> str:
    """Пример времени для подсказки: та же дата через пару дней, полдень."""
    return datetime.fromtimestamp(ts, ZoneInfo(tz)).strftime("%d.%m 12:00")


@requires(Permission.BROADCASTS)
async def msg_when(
    message: Message,
    state: FSMContext,
    ui: Ui,
    broadcast: BroadcastService,
    clock: Clock,
    default_tz: str,
    actor: Actor,
) -> None:
    try:
        send_at = parse_send_time(message.text or "", default_tz, clock.now())
    except ValueError:
        await ui.show(message, ask_screen(T.BC_NEW_TITLE, T.BC_BAD_DATETIME, cb(B, "new")))
        return
    await state.update_data(send_at=send_at)
    await _show_preview(message, state, ui, broadcast, default_tz)


@requires(Permission.BROADCASTS)
async def cb_test(
    callback: CallbackQuery,
    state: FSMContext,
    broadcast: BroadcastService,
    actor: Actor,
) -> None:
    data = await state.get_data()
    text = data.get("text")
    if not isinstance(text, str):
        await callback.answer(T.BC_LOST, show_alert=True)
        return
    delivered = await broadcast.send_test(actor.tg_id, text, _draft_buttons(data))
    await callback.answer(T.BC_TEST_SENT if delivered else T.BC_TEST_FAILED, show_alert=True)


@requires(Permission.BROADCASTS)
async def cb_go(
    callback: CallbackQuery,
    state: FSMContext,
    ui: Ui,
    broadcast: BroadcastService,
    default_tz: str,
    kick_broadcasts: Callable[[], None],
    actor: Actor,
) -> None:
    await callback.answer()
    data = await state.get_data()
    segment, text = data.get("segment"), data.get("text")
    if not isinstance(segment, str) or not isinstance(text, str):
        await state.clear()
        await ui.show(callback, notice_screen(T.BC_LOST, LIST))
        return
    raw_at = data.get("send_at")
    send_at = int(raw_at) if isinstance(raw_at, int) else None
    try:
        broadcast_id = await broadcast.create(
            actor, segment=segment, text=text, buttons=_draft_buttons(data), send_at=send_at
        )
    except ValueError:
        # Например, назначенное время успело пройти, пока администратор проверял текст.
        await ui.show(callback, notice_screen(T.BC_BAD_DATETIME, LIST))
        return
    await state.clear()
    if send_at is None:
        kick_broadcasts()
        flash = fill(T.BC_QUEUED, id=broadcast_id)
    else:
        flash = fill(T.BC_SCHEDULED, id=broadcast_id, when=fmt_datetime(send_at, default_tz))
    item = await broadcast.get(broadcast_id)
    if item is None:
        await ui.show(callback, notice_screen(flash, LIST))
        return
    await ui.show(callback, card_screen(item, default_tz, flash=str(flash)))


# --- обработчики: отмена ---


@requires(Permission.BROADCASTS)
async def cb_cancel_ask(
    callback: CallbackQuery, callback_data: Adm, ui: Ui, broadcast: BroadcastService, actor: Actor
) -> None:
    await callback.answer()
    item = await broadcast.get(callback_data.i)
    if item is None:
        await ui.show(callback, notice_screen(T.ADM_NOT_FOUND, LIST))
        return
    screen = confirm_screen(
        esc(T.BC_CANCEL_CONFIRM.format(id=item.id)),
        yes=cb(B, "cx!", i=item.id),
        back=cb(B, "card", i=item.id),
    )
    await ui.show(callback, screen)


@requires(Permission.BROADCASTS)
async def cb_cancel(
    callback: CallbackQuery,
    callback_data: Adm,
    ui: Ui,
    broadcast: BroadcastService,
    default_tz: str,
    actor: Actor,
) -> None:
    await callback.answer()
    done = await broadcast.cancel(callback_data.i, actor)
    item = await broadcast.get(callback_data.i)
    if item is None:
        await ui.show(callback, notice_screen(T.ADM_NOT_FOUND, LIST))
        return
    flash = str(fill(T.BC_CANCELLED, id=item.id)) if done else T.BC_CANCEL_TOO_LATE
    await ui.show(callback, card_screen(item, default_tz, flash=flash))


def register(router: Router) -> None:
    def only(*actions: str):  # type: ignore[no-untyped-def]
        return Adm.filter((F.s == B) & F.a.in_(set(actions)))

    router.message.register(cmd_broadcast, Command("broadcast"))
    router.callback_query.register(cb_list, only("", "list"))
    router.callback_query.register(cb_card, only("card"))
    router.callback_query.register(cb_new, only("new"))
    router.callback_query.register(cb_segment, only("seg"))
    router.callback_query.register(cb_no_buttons, only("nb"))
    router.callback_query.register(cb_now, only("now"))
    router.callback_query.register(cb_later, only("later"))
    router.callback_query.register(cb_test, only("test"))
    router.callback_query.register(cb_go, only("go"))
    router.callback_query.register(cb_cancel_ask, only("cx"))
    router.callback_query.register(cb_cancel, only("cx!"))

    def typed(state: object, handler: object) -> None:
        router.message.register(
            handler,
            StateFilter(state),
            F.text,
            ~F.text.startswith("/"),  # type: ignore[arg-type]
        )

    typed(BroadcastStates.text, msg_text)
    typed(BroadcastStates.buttons, msg_buttons)
    typed(BroadcastStates.when, msg_when)
