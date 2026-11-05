"""Форматирование чисел и дат для сообщений. Число и единица не разрываются (NBSP)."""

from datetime import datetime
from zoneinfo import ZoneInfo

from plugbackup_bot.domain.plans import GIB

NBSP = " "
_MIB = 1024**2
_KIB = 1024
_MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def gb(value: int) -> str:
    return f"{value}{NBSP}ГБ"


def fmt_bytes(value: int) -> str:
    if value >= GIB:
        return f"{value / GIB:.1f}".replace(".", ",") + f"{NBSP}ГБ"
    if value >= _MIB:
        return f"{value / _MIB:.0f}{NBSP}МБ"
    return f"{value / _KIB:.0f}{NBSP}КБ"


def fmt_date(ts: int, tz: str) -> str:
    moment = datetime.fromtimestamp(ts, ZoneInfo(tz))
    return f"{moment.day} {_MONTHS[moment.month - 1]} {moment.year}"


def fmt_datetime(ts: int, tz: str) -> str:
    moment = datetime.fromtimestamp(ts, ZoneInfo(tz))
    return f"{fmt_date(ts, tz)}, {moment:%H:%M}"


def fmt_iso_date(value: str) -> str:
    """'2026-11-02' -> '2 ноября'."""
    _, month, day = (int(part) for part in value.split("-"))
    return f"{day} {_MONTHS[month - 1]}"


def quota_bar(used: int, quota: int, width: int = 10) -> str:
    """Полоска заполнения квоты: «▰▰▰▱▱▱▱▱▱▱ 30%».

    Если что-то занято, закрашено хотя бы одно деление, а полная полоска только при 100%.
    """
    percent = round(used * 100 / quota) if quota > 0 else 0
    filled = min(width, round(width * percent / 100))
    if used > 0:
        filled = max(filled, 1)
    if percent < 100:
        filled = min(filled, width - 1)
    return "▰" * filled + "▱" * (width - filled) + f" {percent}%"
