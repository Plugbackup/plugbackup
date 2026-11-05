"""Экраны настройки алертов (/alerts)."""

from aiogram.types import InlineKeyboardButton

from plugbackup_bot import texts_ru as T
from plugbackup_bot.domain.alerts import (
    ALERTS,
    BY_KEY,
    COOLDOWN_OPTIONS,
    MANDATORY_NOTICES,
    QUIET_OPTIONS,
    TIMEZONES,
    threshold_label,
)
from plugbackup_bot.emoji import E
from plugbackup_bot.services.alert_settings import SettingsView
from plugbackup_bot.ui import buttons, cards
from plugbackup_bot.ui.buttons import markup
from plugbackup_bot.ui.markup import fill
from plugbackup_bot.ui.screen import Screen

BACK = "al:back"
TOGGLE = "al:t:"
THRESHOLD = "al:th:"
SET_THRESHOLD = "al:sv:"
CONSENT_YES = "al:ip:yes"
QUIET = "al:q"
SET_QUIET = "al:qs:"
TZ = "al:tz"
SET_TZ = "al:tzs:"
COOLDOWN = "al:cd"
SET_COOLDOWN = "al:cds:"
OFF = "al:off:"
# Откуда пришли на экран уведомлений: «Назад» с главного экрана ведёт в статус.
STATUS = "menu:status"


def _back_row() -> list[InlineKeyboardButton]:
    return buttons.nav(BACK)


def _quiet_label(quiet_from: int, quiet_to: int) -> str:
    if quiet_from == quiet_to:
        return T.QUIET_OFF
    return T.QUIET_RANGE.format(start=quiet_from, end=quiet_to)


def _tz_label(name: str) -> str:
    return next((label for label, tz in TIMEZONES if tz == name), name)


def main_screen(view: SettingsView) -> Screen:
    lines: list[str] = []
    rows: list[list[InlineKeyboardButton]] = []
    for alert in ALERTS:
        setting = view.settings[alert.key]
        state = T.ALERT_STATE_ON if setting.enabled else T.ALERT_STATE_OFF
        detail = threshold_label(alert.key, setting.threshold) if alert.options else ""
        suffix = f", {detail}" if detail and setting.enabled else ""
        marker = T.ALERT_CRITICAL_MARKER if alert.critical else ""
        lines.append(
            fill(
                T.ALERT_LINE,
                title=alert.title,
                state=state,
                suffix=suffix,
                description=alert.description,
                marker=marker,
            )
        )
        row = [
            buttons.plain(
                T.ALERT_TOGGLE_BUTTON.format(state=state.capitalize(), title=alert.title),
                f"{TOGGLE}{alert.key}",
            )
        ]
        if alert.options:
            row.append(buttons.plain(T.BTN_THRESHOLD, f"{THRESHOLD}{alert.key}"))
        rows.append(row)

    prefs = view.prefs
    text = cards.card(
        T.ALERTS_TITLE,
        fill(T.ALERTS_MANDATORY, items=", ".join(MANDATORY_NOTICES)),
        cards.bullets(lines),
        cards.lines(
            fill(
                T.ALERTS_QUIET,
                quiet=_quiet_label(prefs.quiet_from, prefs.quiet_to),
                tz=_tz_label(prefs.tz),
            ),
            fill(T.ALERTS_COOLDOWN, hours=prefs.cooldown_hours),
        ),
        icon=E.bell,
    )
    rows.append(
        [
            buttons.plain(T.BTN_QUIET, QUIET),
            buttons.plain(T.BTN_TZ, TZ),
            buttons.plain(T.BTN_COOLDOWN, COOLDOWN),
        ]
    )
    return Screen(text, markup(*rows, buttons.nav(STATUS)))


def threshold_screen(key: str, current: int | None) -> Screen:
    alert = BY_KEY[key]
    now = threshold_label(key, current) or T.THRESHOLD_NOT_SET
    text = fill(T.THRESHOLD_PICK, title=alert.title, now=now)
    rows = [
        [buttons.plain(threshold_label(key, value), f"{SET_THRESHOLD}{key}:{value}")]
        for value in alert.options
    ]
    return Screen(text, markup(*rows, _back_row()))


def quiet_screen() -> Screen:
    rows = [
        [buttons.plain(_quiet_label(*option).capitalize(), f"{SET_QUIET}{i}")]
        for i, option in enumerate(QUIET_OPTIONS)
    ]
    return Screen(T.QUIET_SCREEN, markup(*rows, _back_row()))


def timezone_screen() -> Screen:
    rows = [[buttons.plain(label, f"{SET_TZ}{i}")] for i, (label, _) in enumerate(TIMEZONES)]
    return Screen(T.TZ_SCREEN, markup(*rows, _back_row()))


def cooldown_screen() -> Screen:
    rows = [
        [buttons.plain(T.BTN_COOLDOWN_OPTION.format(hours=hours), f"{SET_COOLDOWN}{hours}")]
        for hours in COOLDOWN_OPTIONS
    ]
    return Screen(T.COOLDOWN_SCREEN, markup(*rows, _back_row()))


def consent_screen() -> Screen:
    text = cards.card(T.CONSENT_TITLE, cards.quote(T.CONSENT_BODY), icon=E.lock)
    return Screen(text, markup([buttons.success(T.BTN_CONSENT, CONSENT_YES)], _back_row()))
