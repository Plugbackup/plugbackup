"""Inline-клавиатуры и разбор callback_data."""

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from plugbackup_bot.domain.plans import Plan
from plugbackup_bot.tg import texts

MENU_BETA = "menu:beta"
MENU_TARIFFS = "menu:tariffs"
MENU_HOW = "menu:how"
MENU_STATUS = "menu:status"
PLAN_PREFIX = "plan:"
RISK_PREFIX = "risk:"
RENEW_PREFIX = "renew:"
ALIVE_PREFIX = "alive:"
RISK_BETA = "beta"


def _markup(*rows: Sequence[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(row) for row in rows])


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def main_menu(has_client: bool) -> InlineKeyboardMarkup:
    rows = [
        [_button(texts.BTN_BETA, MENU_BETA)],
        [_button(texts.BTN_TARIFFS, MENU_TARIFFS), _button(texts.BTN_HOW, MENU_HOW)],
    ]
    if has_client:
        rows.append([_button(texts.BTN_STATUS, MENU_STATUS)])
    return _markup(*rows)


def tariffs(plans: Sequence[Plan], current_plan_id: str | None) -> InlineKeyboardMarkup:
    rows = [
        [_button(f"Выбрать {plan.name}", f"{PLAN_PREFIX}{plan.id}")]
        for plan in plans
        if plan.id != current_plan_id
    ]
    return _markup(*rows)


def risk(target: str) -> InlineKeyboardMarkup:
    return _markup([_button(texts.BTN_RISK_OK, f"{RISK_PREFIX}{target}")])


def pay(url: str, plan: Plan) -> InlineKeyboardMarkup:
    return _markup([InlineKeyboardButton(text=texts.pay_button(plan), url=url)])


def renewal(enabled: bool) -> InlineKeyboardMarkup:
    if enabled:
        return _markup([_button(texts.BTN_RENEWAL_OFF, f"{RENEW_PREFIX}off")])
    return _markup([_button(texts.BTN_RENEWAL_ON, f"{RENEW_PREFIX}on")])


def alive(client_id: int) -> InlineKeyboardMarkup:
    return _markup([_button(texts.BTN_ALIVE, f"{ALIVE_PREFIX}{client_id}")])


def to_tariffs() -> InlineKeyboardMarkup:
    return _markup([_button(texts.BTN_TARIFFS, MENU_TARIFFS)])
