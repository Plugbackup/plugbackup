"""Клиенты: список, поиск, карточка и действия над клиентом."""

import logging
from collections.abc import Callable

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from plugbackup_bot import texts_ru as T
from plugbackup_bot.db.clients import Client
from plugbackup_bot.domain.plans import PlanCatalog
from plugbackup_bot.domain.roles import Actor, Permission
from plugbackup_bot.emoji import E
from plugbackup_bot.services.admin import (
    MAX_BONUS_GB,
    MAX_EXTEND_DAYS,
    MAX_NOTE_LEN,
    ActionKind,
    ActionResult,
    AdminService,
)
from plugbackup_bot.services.notify import Messenger
from plugbackup_bot.tg.admin import core, views
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
from plugbackup_bot.tg.format import fmt_date
from plugbackup_bot.tg.states import ClientAdminStates
from plugbackup_bot.ui import buttons, cards
from plugbackup_bot.ui.markup import esc, fill
from plugbackup_bot.ui.pagination import PAGE_SIZE, Page
from plugbackup_bot.ui.screen import Screen, Ui

log = logging.getLogger(__name__)

MAX_MESSAGE_LEN = 1000
EXTEND_CHOICES = (7, 14, 30, 90)
BONUS_CHOICES = (5, 10, 50, 100)
LIST = cb(core.CLIENTS, "list")


# --- экраны ---


async def card_screen(
    admin: AdminService,
    catalog: PlanCatalog,
    tz: str,
    actor: Actor,
    client_id: int,
    *,
    flash: str | None = None,
) -> Screen:
    """Карточка клиента. flash: строка о результате действия над карточкой."""
    card = await admin.card(client_id)
    if card is None:
        return notice_screen(T.ADM_NOT_FOUND, LIST)
    text = views.client_card(card, catalog, tz)
    if flash:
        text = f"{flash}\n\n{text}"
    return Screen(text, views.client_markup(card, actor, catalog))


async def _list_screen(admin: AdminService, catalog: PlanCatalog, number: int) -> Screen:
    rows, total = await admin.list_clients(limit=PAGE_SIZE, offset=number * PAGE_SIZE)
    pages = max(1, -(-total // PAGE_SIZE))
    if number >= pages:  # страница пропала (клиентов стало меньше): последняя
        number = pages - 1
        rows, total = await admin.list_clients(limit=PAGE_SIZE, offset=number * PAGE_SIZE)
    page = Page(rows, max(number, 0), pages, total)
    items = [
        [
            buttons.plain(
                views.client_row_label(row, catalog), cb(core.CLIENTS, "card", i=row.id), E.user
            )
        ]
        for row in rows
    ]
    body = fill(T.CLIENTS_SUMMARY, total=total) if rows else T.CLIENTS_EMPTY
    markup = buttons.markup(
        [buttons.primary(T.BTN_CLIENT_SEARCH, cb(core.CLIENTS, "find"), E.invite)],
        *items,
        pager(page, core.CLIENTS, "list"),
        admin_nav(cb(core.MENU)),
    )
    return Screen(cards.card(T.CLIENTS_TITLE, body, icon=E.user), markup)


# --- список, поиск, карточка ---


@requires(Permission.CLIENTS_VIEW)
async def cb_list(
    callback: CallbackQuery,
    callback_data: Adm,
    state: FSMContext,
    ui: Ui,
    admin: AdminService,
    catalog: PlanCatalog,
    actor: Actor,
) -> None:
    await state.clear()
    await callback.answer()
    await ui.show(callback, await _list_screen(admin, catalog, callback_data.p))


@requires(Permission.CLIENTS_VIEW)
async def cmd_clients(
    message: Message,
    state: FSMContext,
    ui: Ui,
    admin: AdminService,
    catalog: PlanCatalog,
    actor: Actor,
) -> None:
    await state.clear()
    await ui.show(message, await _list_screen(admin, catalog, 0))


@requires(Permission.CLIENTS_VIEW)
async def cmd_client(
    message: Message,
    command: CommandObject,
    ui: Ui,
    admin: AdminService,
    catalog: PlanCatalog,
    default_tz: str,
    actor: Actor,
) -> None:
    query = (command.args or "").strip()
    if not query:
        await ui.show(message, ask_screen(T.CLIENT_SEARCH_TITLE, T.CLIENT_SEARCH_ASK, LIST))
        return
    client = await admin.find_client(query)
    if client is None:
        await ui.show(message, notice_screen(T.CLIENT_NOT_FOUND, LIST))
        return
    await ui.show(message, await card_screen(admin, catalog, default_tz, actor, client.id))


@requires(Permission.CLIENTS_VIEW)
async def cb_find(callback: CallbackQuery, state: FSMContext, ui: Ui, actor: Actor) -> None:
    await state.set_state(ClientAdminStates.search)
    await callback.answer()
    await ui.show(callback, ask_screen(T.CLIENT_SEARCH_TITLE, T.CLIENT_SEARCH_ASK, LIST))


@requires(Permission.CLIENTS_VIEW)
async def msg_search(
    message: Message,
    state: FSMContext,
    ui: Ui,
    admin: AdminService,
    catalog: PlanCatalog,
    default_tz: str,
    actor: Actor,
) -> None:
    client = await admin.find_client(message.text or "")
    if client is None:
        await ui.show(message, notice_screen(T.CLIENT_NOT_FOUND, LIST))
        return
    await state.clear()
    await ui.show(message, await card_screen(admin, catalog, default_tz, actor, client.id))


@requires(Permission.CLIENTS_VIEW)
async def cb_card(
    callback: CallbackQuery,
    callback_data: Adm,
    state: FSMContext,
    ui: Ui,
    admin: AdminService,
    catalog: PlanCatalog,
    default_tz: str,
    actor: Actor,
) -> None:
    await state.clear()
    await callback.answer()
    screen = await card_screen(admin, catalog, default_tz, actor, callback_data.i)
    await ui.show(callback, screen)


# --- общее для действий ---


class _Context:
    """Всё, что нужно действию над клиентом: сервисы и тексты результата."""

    def __init__(
        self,
        ui: Ui,
        admin: AdminService,
        catalog: PlanCatalog,
        tz: str,
        actor: Actor,
        messenger: Messenger,
        kick: Callable[[], None],
    ) -> None:
        self.ui = ui
        self.admin = admin
        self.catalog = catalog
        self.tz = tz
        self.actor = actor
        self.messenger = messenger
        self.kick = kick

    async def finish(
        self, event: CallbackQuery | Message, result: ActionResult, client_id: int, flash: str
    ) -> None:
        """Показывает карточку с результатом или с причиной отказа."""
        ok = result.kind is ActionKind.OK
        if ok:
            self.kick()
        note = flash if ok or flash else _refusal(result.kind)
        screen = await card_screen(
            self.admin, self.catalog, self.tz, self.actor, client_id, flash=note
        )
        await self.ui.show(event, screen)

    async def tell_client(self, client: Client | None, text: str) -> None:
        if client is not None:
            await self.messenger.send(client.tg_id, text)


def _refusal(kind: ActionKind) -> str:
    return {
        ActionKind.NOT_FOUND: T.ADM_NOT_FOUND,
        ActionKind.WRONG_STATE: T.ADM_STATE_CHANGED,
        ActionKind.SAME_PLAN: T.PLAN_SAME,
        ActionKind.NO_CAPACITY: T.PLAN_NO_CAPACITY,
        ActionKind.TOO_MUCH_DATA: T.PLAN_TOO_MUCH_DATA,
        ActionKind.INVALID: T.ADM_STATE_CHANGED,
    }.get(kind, T.ADM_STATE_CHANGED)


# --- продление ---


@requires(Permission.CLIENTS_EXTEND)
async def cb_extend_menu(
    callback: CallbackQuery,
    callback_data: Adm,
    state: FSMContext,
    ui: Ui,
    admin: AdminService,
    actor: Actor,
) -> None:
    client = await admin.client(callback_data.i)
    await callback.answer()
    if client is None:
        await ui.show(callback, notice_screen(T.ADM_NOT_FOUND, LIST))
        return
    await state.set_state(ClientAdminStates.extend_days)
    await state.update_data(client_id=client.id)
    rows = [
        [
            buttons.plain(
                T.EXTEND_DAYS.format(days=days), cb(core.CLIENTS, "exd", i=client.id, p=days)
            )
            for days in EXTEND_CHOICES
        ]
    ]
    text = cards.card(
        T.EXTEND_TITLE,
        fill(T.EXTEND_ASK, login=client.login, max_days=MAX_EXTEND_DAYS),
        icon=E.clock,
    )
    await ui.show(
        callback,
        Screen(text, buttons.markup(*rows, admin_nav(cb(core.CLIENTS, "card", i=client.id)))),
    )


async def _extend(event: CallbackQuery | Message, ctx: _Context, client_id: int, days: int) -> None:
    result = await ctx.admin.extend(client_id, ctx.actor, days)
    if result.kind is ActionKind.OK and result.client is not None:
        until = result.client.confirm_due_at or result.client.expires_at or 0
        await ctx.tell_client(
            result.client,
            fill(T.CLIENT_NOTICE_EXTENDED, days=days, until=fmt_date(until, ctx.tz)),
        )
        flash = fill(T.EXTEND_RESULT, login=result.client.login, days=days)
    else:
        flash = ""
    await ctx.finish(event, result, client_id, flash)


@requires(Permission.CLIENTS_EXTEND)
async def cb_extend_do(
    callback: CallbackQuery,
    callback_data: Adm,
    state: FSMContext,
    ui: Ui,
    admin: AdminService,
    catalog: PlanCatalog,
    default_tz: str,
    messenger: Messenger,
    kick_provisioning: Callable[[], None],
    actor: Actor,
) -> None:
    await state.clear()
    await callback.answer()
    ctx = _Context(ui, admin, catalog, default_tz, actor, messenger, kick_provisioning)
    await _extend(callback, ctx, callback_data.i, callback_data.p)


@requires(Permission.CLIENTS_EXTEND)
async def msg_extend_days(
    message: Message,
    state: FSMContext,
    ui: Ui,
    admin: AdminService,
    catalog: PlanCatalog,
    default_tz: str,
    messenger: Messenger,
    kick_provisioning: Callable[[], None],
    actor: Actor,
) -> None:
    raw = (message.text or "").strip()
    client_id = int((await state.get_data()).get("client_id", 0))
    if not raw.isdecimal() or not 1 <= int(raw) <= MAX_EXTEND_DAYS:
        hint = T.EXTEND_BAD.format(max_days=MAX_EXTEND_DAYS)
        await ui.show(message, notice_screen(T.ADM_TYPE_ERROR.format(hint=hint), LIST))
        return
    await state.clear()
    ctx = _Context(ui, admin, catalog, default_tz, actor, messenger, kick_provisioning)
    await _extend(message, ctx, client_id, int(raw))


# --- тариф ---


@requires(Permission.CLIENTS_PLAN)
async def cb_plan_menu(
    callback: CallbackQuery,
    callback_data: Adm,
    ui: Ui,
    admin: AdminService,
    catalog: PlanCatalog,
    actor: Actor,
) -> None:
    await callback.answer()
    client = await admin.client(callback_data.i)
    back = cb(core.CLIENTS, "card", i=callback_data.i)
    if client is None:
        await ui.show(callback, notice_screen(T.ADM_NOT_FOUND, LIST))
        return
    if client.plan_id == catalog.beta.id:
        await ui.show(callback, notice_screen(T.PLAN_BETA_NOTE, back))
        return
    others = [plan for plan in catalog.paid if plan.id != client.plan_id]
    if not others:
        await ui.show(callback, notice_screen(T.PLAN_NO_OTHER, back))
        return
    rows = [
        [buttons.plain(plan.name, cb(core.CLIENTS, "pld", i=client.id, x=plan.id), E.payment)]
        for plan in others
    ]
    text = cards.card(T.PLAN_TITLE, fill(T.PLAN_ASK, login=client.login), icon=E.payment)
    await ui.show(callback, Screen(text, buttons.markup(*rows, admin_nav(back))))


@requires(Permission.CLIENTS_PLAN)
async def cb_plan_do(
    callback: CallbackQuery,
    callback_data: Adm,
    ui: Ui,
    admin: AdminService,
    catalog: PlanCatalog,
    default_tz: str,
    messenger: Messenger,
    kick_provisioning: Callable[[], None],
    actor: Actor,
) -> None:
    await callback.answer()
    ctx = _Context(ui, admin, catalog, default_tz, actor, messenger, kick_provisioning)
    result = await admin.change_plan(callback_data.i, actor, callback_data.x)
    plan = catalog.find(callback_data.x)
    flash = ""
    if result.kind is ActionKind.OK and result.client is not None and plan is not None:
        flash = fill(T.PLAN_RESULT, login=result.client.login, name=plan.name)
        await ctx.tell_client(result.client, fill(T.CLIENT_NOTICE_PLAN, name=plan.name))
    await ctx.finish(callback, result, callback_data.i, flash)


# --- бонусные ГБ ---


@requires(Permission.CLIENTS_BONUS)
async def cb_bonus_menu(
    callback: CallbackQuery,
    callback_data: Adm,
    state: FSMContext,
    ui: Ui,
    admin: AdminService,
    actor: Actor,
) -> None:
    await callback.answer()
    client = await admin.client(callback_data.i)
    if client is None:
        await ui.show(callback, notice_screen(T.ADM_NOT_FOUND, LIST))
        return
    await state.set_state(ClientAdminStates.bonus_gb)
    await state.update_data(client_id=client.id)
    adds = [
        buttons.plain(T.BTN_BONUS_ADD.format(gb=gb), cb(core.CLIENTS, "bnd", i=client.id, p=gb))
        for gb in BONUS_CHOICES
    ]
    rows = [adds]
    if client.bonus_gb:
        rows.append(
            [
                buttons.plain(
                    T.BTN_BONUS_RESET, cb(core.CLIENTS, "bnd", i=client.id, p=-client.bonus_gb)
                )
            ]
        )
    text = cards.card(
        T.BONUS_TITLE,
        fill(T.BONUS_ASK, login=client.login, bonus=client.bonus_gb, max_gb=MAX_BONUS_GB),
        icon=E.star,
    )
    back = cb(core.CLIENTS, "card", i=client.id)
    await ui.show(callback, Screen(text, buttons.markup(*rows, admin_nav(back))))


async def _bonus(event: CallbackQuery | Message, ctx: _Context, client_id: int, delta: int) -> None:
    result = await ctx.admin.grant_bonus(client_id, ctx.actor, delta)
    flash = ""
    if result.kind is ActionKind.OK and result.client is not None:
        flash = fill(
            T.BONUS_RESULT,
            login=result.client.login,
            delta=f"{delta:+d}",
            total=result.client.bonus_gb,
        )
        await ctx.tell_client(result.client, fill(T.CLIENT_NOTICE_BONUS, delta=f"{delta:+d}"))
    elif result.kind is ActionKind.INVALID:
        flash = T.BONUS_NEGATIVE
    await ctx.finish(event, result, client_id, flash)


@requires(Permission.CLIENTS_BONUS)
async def cb_bonus_do(
    callback: CallbackQuery,
    callback_data: Adm,
    state: FSMContext,
    ui: Ui,
    admin: AdminService,
    catalog: PlanCatalog,
    default_tz: str,
    messenger: Messenger,
    kick_provisioning: Callable[[], None],
    actor: Actor,
) -> None:
    await state.clear()
    await callback.answer()
    ctx = _Context(ui, admin, catalog, default_tz, actor, messenger, kick_provisioning)
    await _bonus(callback, ctx, callback_data.i, callback_data.p)


@requires(Permission.CLIENTS_BONUS)
async def msg_bonus(
    message: Message,
    state: FSMContext,
    ui: Ui,
    admin: AdminService,
    catalog: PlanCatalog,
    default_tz: str,
    messenger: Messenger,
    kick_provisioning: Callable[[], None],
    actor: Actor,
) -> None:
    raw = (message.text or "").strip()
    try:
        delta = int(raw)
    except ValueError:
        delta = 0
    if delta == 0 or abs(delta) > MAX_BONUS_GB:
        hint = T.BONUS_BAD.format(max_gb=MAX_BONUS_GB)
        await ui.show(message, notice_screen(T.ADM_TYPE_ERROR.format(hint=hint), LIST))
        return
    client_id = int((await state.get_data()).get("client_id", 0))
    await state.clear()
    ctx = _Context(ui, admin, catalog, default_tz, actor, messenger, kick_provisioning)
    await _bonus(message, ctx, client_id, delta)


# --- блокировка, возврат доступа, удаление ---


@requires(Permission.CLIENTS_BLOCK)
async def cb_block_ask(
    callback: CallbackQuery, callback_data: Adm, ui: Ui, admin: AdminService, actor: Actor
) -> None:
    await callback.answer()
    client = await admin.client(callback_data.i)
    if client is None:
        await ui.show(callback, notice_screen(T.ADM_NOT_FOUND, LIST))
        return
    await ui.show(
        callback,
        confirm_screen(
            esc(T.BLOCK_CONFIRM.format(login=client.login)),
            yes=cb(core.CLIENTS, "bk!", i=client.id),
            back=cb(core.CLIENTS, "card", i=client.id),
        ),
    )


@requires(Permission.CLIENTS_BLOCK)
async def cb_block_do(
    callback: CallbackQuery,
    callback_data: Adm,
    ui: Ui,
    admin: AdminService,
    catalog: PlanCatalog,
    default_tz: str,
    messenger: Messenger,
    kick_provisioning: Callable[[], None],
    actor: Actor,
) -> None:
    await callback.answer()
    ctx = _Context(ui, admin, catalog, default_tz, actor, messenger, kick_provisioning)
    result = await admin.suspend(callback_data.i, actor)
    flash = ""
    if result.kind is ActionKind.OK and result.client is not None:
        flash = fill(T.CLIENT_BLOCKED, login=result.client.login)
        await ctx.tell_client(result.client, T.CLIENT_NOTICE_SUSPENDED)
    await ctx.finish(callback, result, callback_data.i, flash)


@requires(Permission.CLIENTS_BLOCK)
async def cb_unblock(
    callback: CallbackQuery,
    callback_data: Adm,
    ui: Ui,
    admin: AdminService,
    catalog: PlanCatalog,
    default_tz: str,
    messenger: Messenger,
    kick_provisioning: Callable[[], None],
    actor: Actor,
) -> None:
    await callback.answer()
    ctx = _Context(ui, admin, catalog, default_tz, actor, messenger, kick_provisioning)
    result = await admin.resume(callback_data.i, actor)
    flash = ""
    if result.kind is ActionKind.OK and result.client is not None:
        flash = fill(T.CLIENT_UNBLOCKED, login=result.client.login)
        await ctx.tell_client(result.client, T.CLIENT_NOTICE_RESUMED)
    await ctx.finish(callback, result, callback_data.i, flash)


@requires(Permission.CLIENTS_DELETE)
async def cb_delete_ask(
    callback: CallbackQuery, callback_data: Adm, ui: Ui, admin: AdminService, actor: Actor
) -> None:
    await callback.answer()
    client = await admin.client(callback_data.i)
    if client is None:
        await ui.show(callback, notice_screen(T.ADM_NOT_FOUND, LIST))
        return
    await ui.show(
        callback,
        confirm_screen(
            esc(T.DELETE_CONFIRM.format(login=client.login)),
            yes=cb(core.CLIENTS, "dl!", i=client.id),
            back=cb(core.CLIENTS, "card", i=client.id),
        ),
    )


@requires(Permission.CLIENTS_DELETE)
async def cb_delete_do(
    callback: CallbackQuery,
    callback_data: Adm,
    ui: Ui,
    admin: AdminService,
    catalog: PlanCatalog,
    default_tz: str,
    messenger: Messenger,
    kick_provisioning: Callable[[], None],
    actor: Actor,
) -> None:
    await callback.answer()
    ctx = _Context(ui, admin, catalog, default_tz, actor, messenger, kick_provisioning)
    result = await admin.delete(callback_data.i, actor)
    flash = ""
    if result.kind is ActionKind.OK and result.client is not None:
        flash = fill(T.CLIENT_DELETED, login=result.client.login)
        await ctx.tell_client(result.client, T.CLIENT_NOTICE_DELETED)
    await ctx.finish(callback, result, callback_data.i, flash)


# --- заметка и сообщение клиенту ---


@requires(Permission.CLIENTS_NOTE)
async def cb_note_start(
    callback: CallbackQuery,
    callback_data: Adm,
    state: FSMContext,
    ui: Ui,
    admin: AdminService,
    actor: Actor,
) -> None:
    await callback.answer()
    client = await admin.client(callback_data.i)
    if client is None:
        await ui.show(callback, notice_screen(T.ADM_NOT_FOUND, LIST))
        return
    await state.set_state(ClientAdminStates.note)
    await state.update_data(client_id=client.id)
    body = fill(T.NOTE_ASK, login=client.login, max_len=MAX_NOTE_LEN)
    await ui.show(callback, ask_screen(T.NOTE_TITLE, body, cb(core.CLIENTS, "card", i=client.id)))


@requires(Permission.CLIENTS_NOTE)
async def msg_note(
    message: Message,
    state: FSMContext,
    ui: Ui,
    admin: AdminService,
    catalog: PlanCatalog,
    default_tz: str,
    actor: Actor,
) -> None:
    text = (message.text or "").strip()
    client_id = int((await state.get_data()).get("client_id", 0))
    clear = text == "-"
    result = await admin.set_note(client_id, actor, "" if clear else text)
    if result.kind is ActionKind.INVALID:
        hint = T.NOTE_BAD.format(max_len=MAX_NOTE_LEN)
        await ui.show(message, notice_screen(T.ADM_TYPE_ERROR.format(hint=hint), LIST))
        return
    await state.clear()
    flash = T.NOTE_CLEARED if clear else T.NOTE_SAVED
    if result.kind is not ActionKind.OK:
        flash = _refusal(result.kind)
    screen = await card_screen(admin, catalog, default_tz, actor, client_id, flash=flash)
    await ui.show(message, screen)


@requires(Permission.CLIENTS_MESSAGE)
async def cb_message_start(
    callback: CallbackQuery,
    callback_data: Adm,
    state: FSMContext,
    ui: Ui,
    admin: AdminService,
    actor: Actor,
) -> None:
    await callback.answer()
    client = await admin.client(callback_data.i)
    if client is None:
        await ui.show(callback, notice_screen(T.ADM_NOT_FOUND, LIST))
        return
    await state.set_state(ClientAdminStates.message)
    await state.update_data(client_id=client.id)
    body = fill(T.MESSAGE_ASK, login=client.login, max_len=MAX_MESSAGE_LEN)
    await ui.show(
        callback, ask_screen(T.MESSAGE_TITLE, body, cb(core.CLIENTS, "card", i=client.id))
    )


@requires(Permission.CLIENTS_MESSAGE)
async def msg_message(
    message: Message,
    state: FSMContext,
    ui: Ui,
    admin: AdminService,
    catalog: PlanCatalog,
    default_tz: str,
    messenger: Messenger,
    actor: Actor,
) -> None:
    text = (message.text or "").strip()
    if not text or len(text) > MAX_MESSAGE_LEN:
        hint = T.MESSAGE_BAD.format(max_len=MAX_MESSAGE_LEN)
        await ui.show(message, notice_screen(T.ADM_TYPE_ERROR.format(hint=hint), LIST))
        return
    client_id = int((await state.get_data()).get("client_id", 0))
    await state.clear()
    card = await admin.card(client_id)
    if card is None:
        await ui.show(message, notice_screen(T.ADM_NOT_FOUND, LIST))
        return
    delivered = await messenger.send(card.client.tg_id, fill(T.CLIENT_MESSAGE, text=text))
    # В журнал идёт только длина: текст сообщения клиенту там не хранится.
    await admin.log(
        actor, "client.message", target_type="client", target_id=client_id,
        length=len(text), delivered=delivered,
    )  # fmt: skip
    flash = T.MESSAGE_SENT if delivered else T.MESSAGE_FAILED
    await ui.show(
        message, await card_screen(admin, catalog, default_tz, actor, client_id, flash=flash)
    )


@requires(Permission.CLIENTS_VIEW)
async def cb_requeue(
    callback: CallbackQuery,
    callback_data: Adm,
    admin: AdminService,
    kick_provisioning: Callable[[], None],
    actor: Actor,
) -> None:
    done = await admin.requeue_job(callback_data.i, actor)
    await callback.answer(T.ADM_DONE if done else T.ADM_NOT_FOUND)
    if done:
        kick_provisioning()


def _only(section: str, action: str) -> object:
    return Adm.filter((F.s == section) & (F.a == action))


def register(router: Router) -> None:
    c = core.CLIENTS
    router.message.register(cmd_clients, Command("clients"))
    router.message.register(cmd_client, Command("client"))
    router.callback_query.register(cb_list, _only(c, "list"))
    router.callback_query.register(cb_find, _only(c, "find"))
    router.callback_query.register(cb_card, _only(c, "card"))
    router.callback_query.register(cb_extend_menu, _only(c, "ex"))
    router.callback_query.register(cb_extend_do, _only(c, "exd"))
    router.callback_query.register(cb_plan_menu, _only(c, "pl"))
    router.callback_query.register(cb_plan_do, _only(c, "pld"))
    router.callback_query.register(cb_bonus_menu, _only(c, "bn"))
    router.callback_query.register(cb_bonus_do, _only(c, "bnd"))
    router.callback_query.register(cb_block_ask, _only(c, "bk"))
    router.callback_query.register(cb_block_do, _only(c, "bk!"))
    router.callback_query.register(cb_unblock, _only(c, "ub"))
    router.callback_query.register(cb_delete_ask, _only(c, "dl"))
    router.callback_query.register(cb_delete_do, _only(c, "dl!"))
    router.callback_query.register(cb_note_start, _only(c, "nt"))
    router.callback_query.register(cb_message_start, _only(c, "ms"))
    router.callback_query.register(cb_requeue, _only(c, "rq"))

    def typed(state: object, handler: object) -> None:
        router.message.register(
            handler,
            StateFilter(state),
            F.text,
            ~F.text.startswith("/"),  # type: ignore[arg-type]
        )

    typed(ClientAdminStates.search, msg_search)
    typed(ClientAdminStates.note, msg_note)
    typed(ClientAdminStates.message, msg_message)
    typed(ClientAdminStates.extend_days, msg_extend_days)
    typed(ClientAdminStates.bonus_gb, msg_bonus)
