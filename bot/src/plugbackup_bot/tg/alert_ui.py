"""Экраны настройки алертов (/alerts)."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from plugbackup_bot.domain.alerts import (
    ALERTS,
    BY_KEY,
    COOLDOWN_OPTIONS,
    MANDATORY_NOTICES,
    QUIET_OPTIONS,
    TIMEZONES,
    threshold_label,
)
from plugbackup_bot.services.alert_settings import SettingsView

Screen = tuple[str, InlineKeyboardMarkup]

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


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _markup(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _back_row() -> list[InlineKeyboardButton]:
    return [_button("Назад", BACK)]


def _quiet_label(quiet_from: int, quiet_to: int) -> str:
    if quiet_from == quiet_to:
        return "выключены"
    return f"{quiet_from:02d}:00–{quiet_to:02d}:00"


def _tz_label(name: str) -> str:
    return next((label for label, tz in TIMEZONES if tz == name), name)


def main_screen(view: SettingsView) -> Screen:
    lines = [
        "Уведомления",
        "",
        "Всегда включены, отключить их нельзя: " + ", ".join(MANDATORY_NOTICES) + ".",
        "",
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for alert in ALERTS:
        setting = view.settings[alert.key]
        state = "вкл" if setting.enabled else "выкл"
        detail = threshold_label(alert.key, setting.threshold) if alert.options else ""
        suffix = f", {detail}" if detail and setting.enabled else ""
        marker = " (приходит и в тихие часы)" if alert.critical else ""
        lines.append(f"• {alert.title} — {state}{suffix}: {alert.description}{marker}")
        row = [_button(f"{state.capitalize()} · {alert.title}", f"{TOGGLE}{alert.key}")]
        if alert.options:
            row.append(_button("Порог", f"{THRESHOLD}{alert.key}"))
        rows.append(row)

    prefs = view.prefs
    lines += [
        "",
        f"Тихие часы: {_quiet_label(prefs.quiet_from, prefs.quiet_to)}, "
        f"пояс: {_tz_label(prefs.tz)}. Критичные алерты приходят всегда.",
        f"Один и тот же алерт — не чаще раза в {prefs.cooldown_hours} ч.",
    ]
    rows.append([_button("Тихие часы", QUIET), _button("Пояс", TZ), _button("Повторы", COOLDOWN)])
    return "\n".join(lines), _markup(rows)


def threshold_screen(key: str, current: int | None) -> Screen:
    alert = BY_KEY[key]
    now = threshold_label(key, current) or "не задан"
    text = f"{alert.title}: выберите порог. Сейчас: {now}."
    rows = [[_button(threshold_label(key, value), f"{SET_THRESHOLD}{key}:{value}")]
            for value in alert.options]  # fmt: skip
    rows.append(_back_row())
    return text, _markup(rows)


def quiet_screen() -> Screen:
    text = "Тихие часы: в это время приходят только критичные алерты, остальные ждут утра."
    rows = [
        [_button(_quiet_label(*option).capitalize(), f"{SET_QUIET}{i}")]
        for i, option in enumerate(QUIET_OPTIONS)
    ]
    rows.append(_back_row())
    return text, _markup(rows)


def timezone_screen() -> Screen:
    text = "Часовой пояс для тихих часов и сводки."
    rows = [[_button(label, f"{SET_TZ}{i}")] for i, (label, _) in enumerate(TIMEZONES)]
    rows.append(_back_row())
    return text, _markup(rows)


def cooldown_screen() -> Screen:
    text = "Как часто может повторяться один и тот же алерт."
    rows = [[_button(f"Раз в {hours} ч", f"{SET_COOLDOWN}{hours}")] for hours in COOLDOWN_OPTIONS]
    rows.append(_back_row())
    return text, _markup(rows)


def consent_screen() -> Screen:
    text = (
        "Алерт о подключении с нового IP-адреса\n\n"
        "Чтобы он работал, нода считает HMAC-хеш IP-адреса каждого подключения. Секрет для "
        "хеша хранится только на ноде. Бот получает хеши, а не адреса: сырые IP ноду не "
        "покидают.\n\n"
        "Бот хранит не больше 20 последних хешей на клиента и не дольше 30 дней. Если "
        "отключить алерт, хеши удаляются сразу.\n\n"
        "PlugBackup не скрывает ваш адрес от ноды: при подключении нода видит IP. Если это "
        "важно, подключайтесь через свой VPN.\n\n"
        "Включайте алерт, только если согласны с этим."
    )
    rows = [[_button("Включить алерт", CONSENT_YES)], _back_row()]
    return text, _markup(rows)
