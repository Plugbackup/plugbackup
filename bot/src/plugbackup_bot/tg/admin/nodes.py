"""Ноды: список, ёмкость, режим обслуживания и добавление новой ноды."""

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from pydantic import ValidationError

from plugbackup_bot import texts_ru as T
from plugbackup_bot.config import NodeConfig
from plugbackup_bot.db.nodes import Node
from plugbackup_bot.domain.roles import Actor, Permission
from plugbackup_bot.emoji import E
from plugbackup_bot.services.admin import AdminService
from plugbackup_bot.services.nodes import MAX_CAPACITY_GB, NodeDetails, NodeResult, NodeService
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
from plugbackup_bot.tg.states import NodeStates
from plugbackup_bot.ui import buttons, cards
from plugbackup_bot.ui.markup import esc, fill
from plugbackup_bot.ui.screen import Screen, Ui

LIST = cb(core.NODES, "list")
# Образец для проверки одного поля: остальные поля подставляются заведомо верные.
_SAMPLE = {"name": "n1", "addr": "192.0.2.1", "public_host": "n1.example.org", "capacity_gb": 0}


def status_of(node: Node) -> str:
    if not node.enabled:
        return "disabled"
    if node.maintenance:
        return "maintenance"
    return "offline" if node.down_since is not None else "online"


def _label(node: Node, sold: int) -> str:
    return fill(
        T.NODE_ROW,
        name=node.name,
        sold=sold,
        limit=node.limit_gb,
        status=T.NODE_STATUS[status_of(node)],
    )


async def list_screen(admin: AdminService) -> Screen:
    loads = await admin.nodes_overview()
    rows = [
        [
            buttons.plain(
                _label(load.node, load.sold_gb), cb(core.NODES, "card", i=load.node.id), E.disk
            )
        ]
        for load in loads
    ]
    body = (
        cards.bullets(_label(load.node, load.sold_gb) for load in loads) if loads else T.NODES_EMPTY
    )
    markup = buttons.markup(
        [buttons.primary(T.BTN_NODE_ADD, cb(core.NODES, "add"), E.invite)],
        *rows,
        admin_nav(cb(core.MENU)),
    )
    return Screen(cards.card(T.NODES_TITLE, body, icon=E.disk), markup)


def card_screen(details: NodeDetails, tz: str, *, flash: str | None = None) -> Screen:
    node = details.node
    state = T.NODE_STATUS[status_of(node)]
    if status_of(node) == "offline" and node.down_since is not None:
        state = f"{state}, {fill(T.NODE_SINCE_DOWN, since=fmt_datetime(node.down_since, tz))}"
    rows = [
        cards.field(T.NODE_ADDR, node.addr),
        cards.field(T.NODE_HOST, node.public_host),
        cards.plain_field(T.NODE_CAPACITY, f"{node.capacity_gb} ГБ"),
        cards.plain_field(T.NODE_OVERSELL, f"{node.oversell:g}"),
        cards.plain_field(T.NODE_LIMIT, f"{node.limit_gb} ГБ"),
        cards.plain_field(T.NODE_SOLD, f"{details.sold_gb} ГБ"),
        cards.plain_field(T.NODE_FREE, f"{details.free_gb} ГБ"),
        cards.plain_field(T.NODE_CLIENTS, details.clients),
        cards.plain_field(T.NODE_STATE, state),
        cards.plain_field(
            T.NODE_SOURCE, T.NODE_SOURCE_ENV if node.source == "env" else T.NODE_SOURCE_BOT
        ),
    ]
    notes = []
    if not node.enabled and node.source == "bot":
        notes.append(T.NODE_DISABLED_NOTE)
    if node.source == "env":
        notes.append(cards.quote(T.NODE_ENV_NOTE))
    text = cards.card(
        T.NODE_CARD_TITLE.format(name=node.name), cards.lines(*rows), *notes, icon=E.disk
    )
    if flash:
        text = f"{flash}\n\n{text}"
    nid = node.id
    keyboard = [
        [
            buttons.plain(T.BTN_NODE_CAPACITY, cb(core.NODES, "cp", i=nid), E.disk),
            buttons.plain(T.BTN_NODE_OVERSELL, cb(core.NODES, "ov", i=nid), E.settings),
        ],
        [
            buttons.success(T.BTN_NODE_MAINT_OFF, cb(core.NODES, "me", i=nid), E.ok)
            if node.maintenance
            else buttons.plain(T.BTN_NODE_MAINT_ON, cb(core.NODES, "mt", i=nid), E.settings)
        ],
        [
            buttons.plain(T.BTN_NODE_CHECK, cb(core.NODES, "ck", i=nid), E.refresh),
            buttons.plain(T.BTN_NODE_DISABLE, cb(core.NODES, "en", i=nid, p=0))
            if node.enabled
            else buttons.success(T.BTN_NODE_ENABLE, cb(core.NODES, "en", i=nid, p=1), E.ok),
        ],
    ]
    return Screen(text, buttons.markup(*keyboard, admin_nav(LIST)))


async def _card(nodes: NodeService, tz: str, node_id: int, flash: str | None = None) -> Screen:
    details = await nodes.details(node_id)
    if details is None:
        return notice_screen(T.ADM_NOT_FOUND, LIST)
    return card_screen(details, tz, flash=flash)


# --- обработчики ---


@requires(Permission.NODES)
async def cmd_nodes(
    message: Message, state: FSMContext, ui: Ui, admin: AdminService, actor: Actor
) -> None:
    await state.clear()
    await ui.show(message, await list_screen(admin))


@requires(Permission.NODES)
async def cb_list(
    callback: CallbackQuery, state: FSMContext, ui: Ui, admin: AdminService, actor: Actor
) -> None:
    await state.clear()
    await callback.answer()
    await ui.show(callback, await list_screen(admin))


@requires(Permission.NODES)
async def cb_card(
    callback: CallbackQuery,
    callback_data: Adm,
    state: FSMContext,
    ui: Ui,
    nodes: NodeService,
    default_tz: str,
    actor: Actor,
) -> None:
    await state.clear()
    await callback.answer()
    await ui.show(callback, await _card(nodes, default_tz, callback_data.i))


@requires(Permission.NODES)
async def cb_edit(
    callback: CallbackQuery,
    callback_data: Adm,
    state: FSMContext,
    ui: Ui,
    nodes: NodeService,
    actor: Actor,
) -> None:
    await callback.answer()
    details = await nodes.details(callback_data.i)
    if details is None:
        await ui.show(callback, notice_screen(T.ADM_NOT_FOUND, LIST))
        return
    node = details.node
    back = cb(core.NODES, "card", i=node.id)
    if callback_data.a == "cp":
        await state.set_state(NodeStates.capacity)
        body = fill(
            T.NODE_ASK_CAPACITY, name=node.name, max=MAX_CAPACITY_GB, current=node.capacity_gb
        )
    else:
        await state.set_state(NodeStates.oversell)
        body = fill(T.NODE_ASK_OVERSELL, name=node.name, current=f"{node.oversell:g}")
    await state.update_data(node_id=node.id)
    await ui.show(callback, ask_screen(T.NODES_TITLE, body, back))


@requires(Permission.NODES)
async def msg_capacity(
    message: Message,
    state: FSMContext,
    ui: Ui,
    nodes: NodeService,
    default_tz: str,
    actor: Actor,
) -> None:
    node_id = int((await state.get_data()).get("node_id", 0))
    raw = (message.text or "").strip()
    if not raw.isdecimal() or int(raw) > MAX_CAPACITY_GB:
        hint = T.NODE_BAD_CAPACITY.format(max=MAX_CAPACITY_GB)
        await ui.show(
            message,
            notice_screen(T.ADM_TYPE_ERROR.format(hint=hint), cb(core.NODES, "card", i=node_id)),
        )
        return
    result = await nodes.set_capacity(node_id, actor, int(raw))
    await state.clear()
    details = await nodes.details(node_id)
    flash = (
        fill(T.NODE_SAVED_CAPACITY, name=details.node.name, value=int(raw))
        if details and result is NodeResult.OK
        else T.ADM_NOT_FOUND
    )
    await ui.show(message, await _card(nodes, default_tz, node_id, flash))


@requires(Permission.NODES)
async def msg_oversell(
    message: Message,
    state: FSMContext,
    ui: Ui,
    nodes: NodeService,
    default_tz: str,
    actor: Actor,
) -> None:
    node_id = int((await state.get_data()).get("node_id", 0))
    try:
        value = float((message.text or "").strip().replace(",", "."))
    except ValueError:
        value = 0.0
    if not 0 < value <= 10:
        await ui.show(
            message,
            notice_screen(
                T.ADM_TYPE_ERROR.format(hint=T.NODE_BAD_OVERSELL), cb(core.NODES, "card", i=node_id)
            ),
        )
        return
    result = await nodes.set_oversell(node_id, actor, value)
    await state.clear()
    details = await nodes.details(node_id)
    flash = (
        fill(T.NODE_SAVED_OVERSELL, name=details.node.name, value=f"{value:g}")
        if details and result is NodeResult.OK
        else T.ADM_NOT_FOUND
    )
    await ui.show(message, await _card(nodes, default_tz, node_id, flash))


# --- связь, включение, обслуживание ---


@requires(Permission.NODES)
async def cb_check(
    callback: CallbackQuery,
    callback_data: Adm,
    ui: Ui,
    nodes: NodeService,
    default_tz: str,
    actor: Actor,
) -> None:
    await callback.answer()
    details = await nodes.details(callback_data.i)
    alive = await nodes.check(callback_data.i)
    if details is None or alive is None:
        await ui.show(callback, notice_screen(T.ADM_NOT_FOUND, LIST))
        return
    template = T.NODE_CHECK_OK if alive else T.NODE_CHECK_FAIL
    flash = fill(template, name=details.node.name)
    await ui.show(callback, await _card(nodes, default_tz, callback_data.i, flash))


@requires(Permission.NODES)
async def cb_enable(
    callback: CallbackQuery,
    callback_data: Adm,
    ui: Ui,
    nodes: NodeService,
    default_tz: str,
    actor: Actor,
) -> None:
    await callback.answer()
    enabled = bool(callback_data.p)
    result = await nodes.set_enabled(callback_data.i, actor, enabled)
    details = await nodes.details(callback_data.i)
    if result is NodeResult.OK and details is not None:
        flash = fill(T.NODE_ENABLED if enabled else T.NODE_DISABLED, name=details.node.name)
    elif result is NodeResult.UNREACHABLE:
        flash = T.NODE_UNREACHABLE
    else:
        flash = T.ADM_NOT_FOUND
    await ui.show(callback, await _card(nodes, default_tz, callback_data.i, flash))


@requires(Permission.NODES)
async def cb_maint_ask(
    callback: CallbackQuery, callback_data: Adm, ui: Ui, nodes: NodeService, actor: Actor
) -> None:
    await callback.answer()
    details = await nodes.details(callback_data.i)
    if details is None:
        await ui.show(callback, notice_screen(T.ADM_NOT_FOUND, LIST))
        return
    starting = callback_data.a == "mt"
    template = T.NODE_MAINT_CONFIRM if starting else T.NODE_MAINT_END_CONFIRM
    body = esc(template.format(name=details.node.name, count=details.clients))
    screen = confirm_screen(
        body,
        yes=cb(core.NODES, "mt!" if starting else "me!", i=callback_data.i),
        back=cb(core.NODES, "card", i=callback_data.i),
    )
    await ui.show(callback, screen)


@requires(Permission.NODES)
async def cb_maint_do(
    callback: CallbackQuery,
    callback_data: Adm,
    ui: Ui,
    nodes: NodeService,
    default_tz: str,
    actor: Actor,
) -> None:
    await callback.answer()
    details = await nodes.details(callback_data.i)
    if details is None:
        await ui.show(callback, notice_screen(T.ADM_NOT_FOUND, LIST))
        return
    name = details.node.name
    if callback_data.a == "mt!":
        result = await nodes.start_maintenance(
            callback_data.i, actor, T.NODE_MAINT_TEXT.format(name=name)
        )
        done = T.NODE_MAINT_STARTED
    else:
        result = await nodes.end_maintenance(callback_data.i, actor)
        done = T.NODE_MAINT_ENDED
    flash = fill(done, name=name) if result is NodeResult.OK else T.NODE_STATE_CHANGED
    await ui.show(callback, await _card(nodes, default_tz, callback_data.i, flash))


# --- добавление ноды ---


def _problem(field: str, value: object) -> str | None:
    """Почему значение не годится для поля ноды, или None."""
    try:
        NodeConfig.model_validate({**_SAMPLE, field: value})
    except ValidationError as error:
        for item in error.errors():
            if item["loc"] and item["loc"][0] == field:
                return str(item["msg"]).removeprefix("Value error, ")
        return T.NODE_ADD_BAD_NUMBER
    return None


@requires(Permission.NODES)
async def cb_add(callback: CallbackQuery, state: FSMContext, ui: Ui, actor: Actor) -> None:
    await state.set_state(NodeStates.add_name)
    await state.update_data(new={})
    await callback.answer()
    await ui.show(callback, ask_screen(T.NODE_ADD_TITLE, T.NODE_ADD_NAME, LIST))


async def _step(
    message: Message,
    state: FSMContext,
    ui: Ui,
    field: str,
    next_state: object,
    ask: str,
) -> None:
    value = (message.text or "").strip()
    problem = _problem(field, value)
    if problem is not None:
        await ui.show(
            message, ask_screen(T.NODE_ADD_TITLE, T.NODE_ADD_BAD.format(problem=problem), LIST)
        )
        return
    data = (await state.get_data()).get("new", {})
    data[field] = value
    await state.update_data(new=data)
    await state.set_state(next_state)  # type: ignore[arg-type]
    await ui.show(message, ask_screen(T.NODE_ADD_TITLE, ask, LIST))


@requires(Permission.NODES)
async def msg_add_name(message: Message, state: FSMContext, ui: Ui, actor: Actor) -> None:
    await _step(message, state, ui, "name", NodeStates.add_addr, T.NODE_ADD_ADDR)


@requires(Permission.NODES)
async def msg_add_addr(message: Message, state: FSMContext, ui: Ui, actor: Actor) -> None:
    await _step(message, state, ui, "addr", NodeStates.add_host, T.NODE_ADD_HOST)


@requires(Permission.NODES)
async def msg_add_host(message: Message, state: FSMContext, ui: Ui, actor: Actor) -> None:
    ask = fill(T.NODE_ADD_CAPACITY, max=MAX_CAPACITY_GB)
    await _step(message, state, ui, "public_host", NodeStates.add_capacity, ask)


@requires(Permission.NODES)
async def msg_add_capacity(
    message: Message,
    state: FSMContext,
    ui: Ui,
    nodes: NodeService,
    default_tz: str,
    actor: Actor,
) -> None:
    raw = (message.text or "").strip()
    if not raw.isdecimal() or int(raw) > MAX_CAPACITY_GB:
        problem = T.NODE_ADD_BAD_NUMBER
        await ui.show(
            message, ask_screen(T.NODE_ADD_TITLE, T.NODE_ADD_BAD.format(problem=problem), LIST)
        )
        return
    data = (await state.get_data()).get("new", {})
    await state.clear()
    try:
        config = NodeConfig(
            name=data["name"],
            addr=data["addr"],
            public_host=data["public_host"],
            capacity_gb=int(raw),
        )
    except (KeyError, ValidationError):
        await ui.show(message, notice_screen(T.ADM_STALE, LIST))
        return
    result, node_id = await nodes.add(actor, config)
    if result is NodeResult.EXISTS or node_id is None:
        await ui.show(message, notice_screen(T.NODE_ADD_EXISTS, LIST))
        return
    flash = fill(T.NODE_ADD_DONE, name=config.name)
    await ui.show(message, await _card(nodes, default_tz, node_id, flash))


def register(router: Router) -> None:
    n = core.NODES

    def only(*actions: str):  # type: ignore[no-untyped-def]
        return Adm.filter((F.s == n) & F.a.in_(set(actions)))

    router.message.register(cmd_nodes, Command("nodes"))
    router.callback_query.register(cb_list, only("", "list"))
    router.callback_query.register(cb_card, only("card"))
    router.callback_query.register(cb_edit, only("cp", "ov"))
    router.callback_query.register(cb_check, only("ck"))
    router.callback_query.register(cb_enable, only("en"))
    router.callback_query.register(cb_maint_ask, only("mt", "me"))
    router.callback_query.register(cb_maint_do, only("mt!", "me!"))
    router.callback_query.register(cb_add, only("add"))

    def typed(state: object, handler: object) -> None:
        router.message.register(
            handler,
            StateFilter(state),
            F.text,
            ~F.text.startswith("/"),  # type: ignore[arg-type]
        )

    typed(NodeStates.capacity, msg_capacity)
    typed(NodeStates.oversell, msg_oversell)
    typed(NodeStates.add_name, msg_add_name)
    typed(NodeStates.add_addr, msg_add_addr)
    typed(NodeStates.add_host, msg_add_host)
    typed(NodeStates.add_capacity, msg_add_capacity)
