"""Общие куски экранов поддержки: ответ клиенту и подпись «кто написал»."""

from aiogram.types import InlineKeyboardMarkup

from plugbackup_bot import texts_ru as T
from plugbackup_bot.emoji import E
from plugbackup_bot.ui import buttons, cards
from plugbackup_bot.ui.buttons import markup
from plugbackup_bot.ui.markup import esc, fill

START = "sup:start"
CLOSE = "sup:close"


def answer_message(ticket_id: int, text: str) -> str:
    """Ответ поддержки клиенту."""
    return cards.card(str(fill(T.SUP_ANSWER_TITLE, id=ticket_id)), esc(text), icon=E.support)


def answer_markup() -> InlineKeyboardMarkup:
    return markup(
        [buttons.primary(T.BTN_SUP_REPLY, START, E.support)],
        [buttons.plain(T.BTN_SUP_CLOSE, CLOSE, E.trash)],
    )


def who(login: str | None, username: str | None, tg_id: int) -> str:
    """«u1001 · @name · 123». Без доступа вместо логина стоит пометка."""
    parts = [login or T.SUPA_NO_CLIENT, f"@{username}" if username else "", str(tg_id)]
    return " · ".join(part for part in parts if part)
