"""Тарифы: цена в Stars, объём, ориентир в рублях, видимость. Применяются без перезапуска."""

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from plugbackup_bot import texts_ru as T
from plugbackup_bot.config import MAX_PRICE_STARS
from plugbackup_bot.domain.plans import Plan, PlanCatalog
from plugbackup_bot.domain.roles import Actor, Permission
from plugbackup_bot.emoji import E
from plugbackup_bot.services.plans import MAX_GB, MAX_PRICE_RUB, PlanResult, PlanService
from plugbackup_bot.tg.admin import core
from plugbackup_bot.tg.admin.core import Adm, admin_nav, ask_screen, cb, notice_screen, requires
from plugbackup_bot.tg.states import TariffStates
from plugbackup_bot.ui import buttons, cards
from plugbackup_bot.ui.markup import fill
from plugbackup_bot.ui.screen import Screen, Ui

LIST = cb(core.TARIFFS, "list")

# Поле тарифа по действию кнопки: (состояние ввода, подпись, запрос, ответ, границы).
_FIELDS = {
    "st": ("stars", T.TAR_ASK_STARS, T.TAR_SAVED_STARS, 1, MAX_PRICE_STARS),
    "gb": ("gb", T.TAR_ASK_GB, T.TAR_SAVED_GB, 1, MAX_GB),
    "rb": ("rub", T.TAR_ASK_RUB, T.TAR_SAVED_RUB, 1, MAX_PRICE_RUB),
}


def _status(plan: Plan) -> str:
    return T.TAR_VISIBLE if plan.visible else T.TAR_HIDDEN


def _line(plan: Plan) -> str:
    if plan.is_beta:
        return fill(T.TAR_LINE_BETA, name=plan.name, gb=plan.gb)
    return fill(
        T.TAR_LINE, name=plan.name, rub=plan.price_rub, stars=plan.stars, status=_status(plan)
    )


def list_screen(catalog: PlanCatalog) -> Screen:
    plans = catalog.plans
    rows = []
    for plan in plans:
        label = (
            fill(T.TAR_ROW_BETA, name=plan.name)
            if plan.is_beta
            else fill(T.TAR_ROW, name=plan.name, stars=plan.stars, status=_status(plan))
        )
        rows.append([buttons.plain(label, cb(core.TARIFFS, "card", x=plan.id), E.payment)])
    note = None if catalog.paid else T.TAR_ALL_HIDDEN
    text = cards.card(
        T.TAR_TITLE,
        cards.bullets(_line(plan) for plan in plans),
        note,
        cards.quote(f"{T.TAR_NOTE}\n\n{T.TAR_BANNER_NOTE}"),
        icon=E.payment,
    )
    return Screen(text, buttons.markup(*rows, admin_nav(cb(core.MENU))))


async def card_screen(
    plans: PlanService, catalog: PlanCatalog, plan_id: str, *, flash: str | None = None
) -> Screen:
    plan = catalog.find(plan_id)
    if plan is None:
        return notice_screen(T.ADM_NOT_FOUND, LIST)
    counts = await plans.client_counts()
    rows = [
        cards.field(T.TAR_ID, plan.id),
        cards.plain_field(T.TAR_GB, f"{plan.gb} ГБ"),
    ]
    if not plan.is_beta:
        rows += [
            cards.plain_field(T.TAR_STARS, plan.stars),
            cards.plain_field(T.TAR_RUB, f"{plan.price_rub} ₽"),
            cards.plain_field(T.TAR_STATUS, _status(plan)),
        ]
    rows.append(cards.plain_field(T.TAR_CLIENTS, counts.get(plan.id, 0)))
    text = cards.card(
        T.TAR_CARD_TITLE.format(name=plan.name),
        cards.lines(*rows),
        T.TAR_BETA_NOTE if plan.is_beta else cards.quote(T.TAR_NOTE),
        icon=E.payment,
    )
    if flash:
        text = f"{flash}\n\n{text}"
    keyboard = []
    if not plan.is_beta:
        keyboard.append(
            [
                buttons.plain(T.BTN_TAR_STARS, cb(core.TARIFFS, "st", x=plan.id), E.star),
                buttons.plain(T.BTN_TAR_RUB, cb(core.TARIFFS, "rb", x=plan.id), E.payment),
            ]
        )
    gb_row = [buttons.plain(T.BTN_TAR_GB, cb(core.TARIFFS, "gb", x=plan.id), E.disk)]
    if not plan.is_beta:
        gb_row.append(
            buttons.plain(T.BTN_TAR_HIDE, cb(core.TARIFFS, "vis", x=plan.id, p=0))
            if plan.visible
            else buttons.success(T.BTN_TAR_SHOW, cb(core.TARIFFS, "vis", x=plan.id, p=1), E.ok)
        )
    keyboard.append(gb_row)
    return Screen(text, buttons.markup(*keyboard, admin_nav(LIST)))


def _refusal(result: PlanResult, field: str) -> str:
    return {
        PlanResult.NOT_FOUND: T.ADM_NOT_FOUND,
        PlanResult.DUPLICATE_GB: T.TAR_DUPLICATE_GB,
        PlanResult.BETA: T.TAR_BETA_REFUSED,
    }.get(result, T.ADM_STATE_CHANGED)


# --- обработчики ---


@requires(Permission.TARIFFS)
async def cmd_tariffs(
    message: Message, state: FSMContext, ui: Ui, catalog: PlanCatalog, actor: Actor
) -> None:
    await state.clear()
    await ui.show(message, list_screen(catalog))


@requires(Permission.TARIFFS)
async def cb_list(
    callback: CallbackQuery, state: FSMContext, ui: Ui, catalog: PlanCatalog, actor: Actor
) -> None:
    await state.clear()
    await callback.answer()
    await ui.show(callback, list_screen(catalog))


@requires(Permission.TARIFFS)
async def cb_card(
    callback: CallbackQuery,
    callback_data: Adm,
    state: FSMContext,
    ui: Ui,
    plans: PlanService,
    catalog: PlanCatalog,
    actor: Actor,
) -> None:
    await state.clear()
    await callback.answer()
    await ui.show(callback, await card_screen(plans, catalog, callback_data.x))


@requires(Permission.TARIFFS)
async def cb_edit(
    callback: CallbackQuery,
    callback_data: Adm,
    state: FSMContext,
    ui: Ui,
    catalog: PlanCatalog,
    actor: Actor,
) -> None:
    await callback.answer()
    plan = catalog.find(callback_data.x)
    if plan is None or callback_data.a not in _FIELDS:
        await ui.show(callback, notice_screen(T.ADM_NOT_FOUND, LIST))
        return
    field, ask, _, _, high = _FIELDS[callback_data.a]
    current = {"stars": plan.stars, "gb": plan.gb, "rub": plan.price_rub}[field]
    await state.set_state(getattr(TariffStates, field))
    await state.update_data(plan_id=plan.id, action=callback_data.a)
    body = fill(ask, name=plan.name, max=high, current=current)
    await ui.show(callback, ask_screen(T.TAR_TITLE, body, cb(core.TARIFFS, "card", x=plan.id)))


@requires(Permission.TARIFFS)
async def msg_value(
    message: Message,
    state: FSMContext,
    ui: Ui,
    plans: PlanService,
    catalog: PlanCatalog,
    actor: Actor,
) -> None:
    data = await state.get_data()
    action, plan_id = data.get("action", ""), data.get("plan_id", "")
    if action not in _FIELDS:
        await state.clear()
        return
    field, _, saved, low, high = _FIELDS[action]
    raw = (message.text or "").strip()
    if not raw.isdecimal() or not low <= int(raw) <= high:
        hint = T.TAR_BAD_NUMBER.format(low=low, high=high)
        await ui.show(
            message,
            notice_screen(T.ADM_TYPE_ERROR.format(hint=hint), cb(core.TARIFFS, "card", x=plan_id)),
        )
        return
    value = int(raw)
    setter = {"stars": plans.set_stars, "gb": plans.set_gb, "rub": plans.set_price_rub}[field]
    result = await setter(plan_id, actor, value)
    await state.clear()
    flash = (
        fill(saved, name=catalog.get(plan_id).name, value=value)
        if result is PlanResult.OK
        else _refusal(result, field)
    )
    await ui.show(message, await card_screen(plans, catalog, plan_id, flash=flash))


@requires(Permission.TARIFFS)
async def cb_visibility(
    callback: CallbackQuery,
    callback_data: Adm,
    ui: Ui,
    plans: PlanService,
    catalog: PlanCatalog,
    actor: Actor,
) -> None:
    await callback.answer()
    visible = bool(callback_data.p)
    result = await plans.set_visible(callback_data.x, actor, visible)
    plan = catalog.find(callback_data.x)
    if result is PlanResult.OK and plan is not None:
        flash = fill(T.TAR_SAVED_SHOWN if visible else T.TAR_SAVED_HIDDEN, name=plan.name)
    else:
        flash = _refusal(result, "visible")
    await ui.show(callback, await card_screen(plans, catalog, callback_data.x, flash=flash))


def register(router: Router) -> None:
    t = core.TARIFFS
    router.message.register(cmd_tariffs, Command("tariffs"))
    router.callback_query.register(cb_list, Adm.filter((F.s == t) & (F.a == "list")))
    router.callback_query.register(cb_card, Adm.filter((F.s == t) & (F.a == "card")))
    router.callback_query.register(cb_edit, Adm.filter((F.s == t) & F.a.in_(set(_FIELDS))))
    router.callback_query.register(cb_visibility, Adm.filter((F.s == t) & (F.a == "vis")))
    for state in (TariffStates.stars, TariffStates.gb, TariffStates.rub):
        router.message.register(msg_value, StateFilter(state), F.text, ~F.text.startswith("/"))
