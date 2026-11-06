"""Клавиатуры клиента и разбор callback_data.

Цвет кнопки задаётся смыслом (см. ui.buttons): зелёная — оплатить и подтвердить, красная —
отказаться, синяя — главное действие экрана. Первый уровень меню заканчивается кнопкой
«Главное меню», глубже добавляется «Назад».
"""

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from plugbackup_bot import texts_ru as T
from plugbackup_bot.domain.plans import Plan
from plugbackup_bot.domain.states import State
from plugbackup_bot.emoji import E
from plugbackup_bot.services.access import StatusView
from plugbackup_bot.tg import texts
from plugbackup_bot.ui import buttons
from plugbackup_bot.ui.buttons import markup

MENU_HOME = buttons.HOME_DATA
MENU_BETA = "menu:beta"
MENU_TARIFFS = "menu:tariffs"
MENU_HOW = "menu:how"
MENU_STATUS = "menu:status"
MENU_KEY = "menu:key"
PLAN_PREFIX = "plan:"
RISK_PREFIX = "risk:"
RENEW_PREFIX = "renew:"
ALIVE_PREFIX = "alive:"
RISK_BETA = "beta"
# Экраны других разделов, на которые ведут кнопки статуса.
ALERTS_HOME = "al:back"
RESTORE_START = "restore:start"
SUPPORT_START = "sup:start"


def main_menu(has_client: bool) -> InlineKeyboardMarkup:
    if has_client:
        first = [buttons.primary(T.BTN_STATUS, MENU_STATUS, E.chart)]
        second = [
            buttons.plain(T.BTN_TARIFFS, MENU_TARIFFS, E.payment),
            buttons.plain(T.BTN_HOW, MENU_HOW),
        ]
        third = [
            buttons.plain(T.BTN_BETA, MENU_BETA, E.invite),
            buttons.plain(T.BTN_SUPPORT, SUPPORT_START, E.support),
        ]
        return markup(first, second, third)
    return markup(
        [buttons.primary(T.BTN_TARIFFS, MENU_TARIFFS, E.payment)],
        [buttons.plain(T.BTN_BETA, MENU_BETA, E.invite), buttons.plain(T.BTN_HOW, MENU_HOW)],
        [buttons.plain(T.BTN_SUPPORT, SUPPORT_START, E.support)],
    )


def only_home() -> InlineKeyboardMarkup:
    return markup(buttons.nav())


def tariffs(plans: Sequence[Plan], current_plan_id: str | None) -> InlineKeyboardMarkup:
    rows = [
        [buttons.plain(T.BTN_CHOOSE_PLAN.format(name=plan.name), f"{PLAN_PREFIX}{plan.id}", E.star)]
        for plan in plans
        if plan.id != current_plan_id
    ]
    return markup(*rows, buttons.nav())


def risk(target: str) -> InlineKeyboardMarkup:
    """Экран рисков. Из беты «Назад» ведёт в меню, из платного тарифа к тарифам."""
    back = None if target == RISK_BETA else MENU_TARIFFS
    return markup(
        [buttons.success(T.BTN_RISK_OK, f"{RISK_PREFIX}{target}", E.ok)], buttons.nav(back)
    )


def pay(url: str, plan: Plan) -> InlineKeyboardMarkup:
    pay_button = buttons.link(texts.pay_button(plan), url, E.payment, buttons.SUCCESS)
    return markup([pay_button], buttons.nav(MENU_TARIFFS))


def key_help() -> InlineKeyboardMarkup:
    """Подсказка про ключ: команды копируются нажатием."""
    return markup(
        [
            buttons.copy_button(T.BTN_COPY_KEY_UNIX, T.KEY_COMMAND_UNIX),
            buttons.copy_button(T.BTN_COPY_KEY_WINDOWS, T.KEY_COMMAND_WINDOWS),
        ],
        buttons.nav(),
    )


def access_ready(login: str, host: str) -> InlineKeyboardMarkup:
    init, backup = texts.restic_commands()
    return markup(
        [
            buttons.copy_button(T.BTN_COPY_LOGIN, login, E.user),
            buttons.copy_button(T.BTN_COPY_HOST, host, E.upload),
        ],
        [buttons.copy_button(T.BTN_COPY_RESTIC_INIT, init)],
        [buttons.copy_button(T.BTN_COPY_RESTIC_BACKUP, backup)],
        buttons.nav(),
    )


def renewal_button(enabled: bool) -> InlineKeyboardButton:
    if enabled:
        return buttons.danger(T.BTN_RENEWAL_OFF, f"{RENEW_PREFIX}off")
    return buttons.success(T.BTN_RENEWAL_ON, f"{RENEW_PREFIX}on", E.refresh)


def status(view: StatusView) -> InlineKeyboardMarkup:
    client = view.client
    rows: list[list[InlineKeyboardButton]] = []
    if client.state is State.PENDING and client.ssh_key_body is None:
        rows.append([buttons.primary(T.BTN_SEND_KEY, MENU_KEY, E.key)])
    # Переключатель нужен тем, у кого есть подписка Stars (бета и инвайты без неё).
    if client.sub_charge_id is not None:
        rows.append([renewal_button(client.renewal_on)])
    if view.node is not None:
        rows.append(
            [
                buttons.copy_button(T.BTN_COPY_LOGIN, client.login, E.user),
                buttons.copy_button(T.BTN_COPY_HOST, view.node.public_host, E.upload),
            ]
        )
    rows.append(
        [
            buttons.plain(T.BTN_UPGRADE, MENU_TARIFFS, E.payment),
            buttons.plain(T.BTN_KEY, MENU_KEY, E.key),
        ]
    )
    rows.append(
        [
            buttons.plain(T.BTN_ALERTS, ALERTS_HOME, E.bell),
            buttons.plain(T.BTN_RESTORE_OPEN, RESTORE_START, E.refresh),
        ]
    )
    return markup(*rows, buttons.nav())


def alive(client_id: int) -> InlineKeyboardMarkup:
    return markup([buttons.success(T.BTN_ALIVE, f"{ALIVE_PREFIX}{client_id}", E.ok)])


def to_tariffs() -> InlineKeyboardMarkup:
    return markup([buttons.primary(T.BTN_TARIFFS, MENU_TARIFFS, E.payment)])
