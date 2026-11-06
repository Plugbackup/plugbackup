"""Режим техработ: плашка во всех ответах бота и рассылка в начале и конце."""

from collections.abc import Callable

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from plugbackup_bot import texts_ru as T
from plugbackup_bot.domain.roles import Actor, Permission
from plugbackup_bot.emoji import E
from plugbackup_bot.services.broadcast import BroadcastService, Segment
from plugbackup_bot.services.maintenance_mode import MAX_TEXT, MaintenanceMode, ModeKind
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
from plugbackup_bot.tg.format import fmt_datetime
from plugbackup_bot.tg.states import MaintenanceStates
from plugbackup_bot.ui import buttons, cards
from plugbackup_bot.ui.markup import esc, fill
from plugbackup_bot.ui.screen import Screen, Ui

W = core.MAINT
HOME = cb(W, "home")


def status_screen(mode: MaintenanceMode, tz: str, *, flash: str | None = None) -> Screen:
    state = mode.state
    if state is None:
        body = cards.lines(T.MAINT_OFF, "", T.MAINT_EXPLAIN)
        rows = [[buttons.primary(T.BTN_MAINT_START, cb(W, "on"), E.settings)]]
        icon = E.settings
    else:
        body = cards.lines(
            T.MAINT_ON,
            cards.plain_field(T.MAINT_ON_TEXT, state.text),
            fill(T.MAINT_ON_SINCE, since=fmt_datetime(state.since, tz)),
        )
        rows = [[buttons.danger(T.BTN_MAINT_END, cb(W, "off"), E.ok)]]
        icon = E.warning
    text = cards.card(T.MAINT_TITLE, body, icon=icon)
    if flash:
        text = f"{flash}\n\n{text}"
    return Screen(text, buttons.markup(*rows, admin_nav(cb(core.MENU))))


@requires(Permission.MAINTENANCE)
async def cb_home(
    callback: CallbackQuery,
    state: FSMContext,
    ui: Ui,
    maintenance: MaintenanceMode,
    default_tz: str,
    actor: Actor,
) -> None:
    await state.clear()
    await callback.answer()
    await ui.show(callback, status_screen(maintenance, default_tz))


@requires(Permission.MAINTENANCE)
async def cb_on(
    callback: CallbackQuery,
    state: FSMContext,
    ui: Ui,
    maintenance: MaintenanceMode,
    default_tz: str,
    actor: Actor,
) -> None:
    await callback.answer()
    if maintenance.active:
        await ui.show(callback, status_screen(maintenance, default_tz, flash=T.MAINT_ALREADY_ON))
        return
    await state.clear()
    await state.set_state(MaintenanceStates.text)
    body = fill(T.MAINT_ASK_TEXT, max=MAX_TEXT)
    await ui.show(callback, ask_screen(T.MAINT_TITLE, body, HOME))


@requires(Permission.MAINTENANCE)
async def msg_text(
    message: Message, state: FSMContext, ui: Ui, broadcast: BroadcastService, actor: Actor
) -> None:
    text = " ".join((message.text or "").split())
    if not 1 <= len(text) <= MAX_TEXT:
        body = fill(T.MAINT_BAD_TEXT, max=MAX_TEXT)
        await ui.show(message, ask_screen(T.MAINT_TITLE, body, HOME))
        return
    await state.update_data(text=text)
    await state.set_state(None)
    count = len(await broadcast.recipients(Segment.ALL))
    await ui.show(message, _confirm_start(text, count))


def _confirm_start(text: str, count: int) -> Screen:
    body = cards.lines(esc(T.MAINT_START_CONFIRM.format(count=count)), cards.quote(esc(text)))
    return confirm_screen(body, yes=cb(W, "on!"), back=HOME)


@requires(Permission.MAINTENANCE)
async def cb_start(
    callback: CallbackQuery,
    state: FSMContext,
    ui: Ui,
    maintenance: MaintenanceMode,
    kick_broadcasts: Callable[[], None],
    default_tz: str,
    actor: Actor,
) -> None:
    await callback.answer()
    text = (await state.get_data()).get("text")
    if not isinstance(text, str):
        await ui.show(callback, notice_screen(T.MAINT_LOST, HOME))
        return
    result = await maintenance.start(actor, text)
    await state.clear()
    if result is ModeKind.OK:
        kick_broadcasts()
        flash = T.MAINT_STARTED
    elif result is ModeKind.ALREADY:
        flash = T.MAINT_ALREADY_ON
    else:
        flash = T.MAINT_LOST
    await ui.show(callback, status_screen(maintenance, default_tz, flash=flash))


@requires(Permission.MAINTENANCE)
async def cb_end_ask(
    callback: CallbackQuery,
    ui: Ui,
    broadcast: BroadcastService,
    maintenance: MaintenanceMode,
    default_tz: str,
    actor: Actor,
) -> None:
    await callback.answer()
    if not maintenance.active:
        await ui.show(callback, status_screen(maintenance, default_tz, flash=T.MAINT_ALREADY_OFF))
        return
    count = len(await broadcast.recipients(Segment.ALL))
    body = esc(T.MAINT_END_CONFIRM.format(count=count))
    await ui.show(callback, confirm_screen(body, yes=cb(W, "off!"), back=HOME))


@requires(Permission.MAINTENANCE)
async def cb_end(
    callback: CallbackQuery,
    ui: Ui,
    maintenance: MaintenanceMode,
    kick_broadcasts: Callable[[], None],
    default_tz: str,
    actor: Actor,
) -> None:
    await callback.answer()
    result = await maintenance.end(actor)
    if result is ModeKind.OK:
        kick_broadcasts()
        flash = T.MAINT_ENDED
    else:
        flash = T.MAINT_ALREADY_OFF
    await ui.show(callback, status_screen(maintenance, default_tz, flash=flash))


def register(router: Router) -> None:
    def only(*actions: str):  # type: ignore[no-untyped-def]
        return Adm.filter((F.s == W) & F.a.in_(set(actions)))

    router.callback_query.register(cb_home, only("", "home"))
    router.callback_query.register(cb_on, only("on"))
    router.callback_query.register(cb_start, only("on!"))
    router.callback_query.register(cb_end_ask, only("off"))
    router.callback_query.register(cb_end, only("off!"))
    router.message.register(
        msg_text, StateFilter(MaintenanceStates.text), F.text, ~F.text.startswith("/")
    )
