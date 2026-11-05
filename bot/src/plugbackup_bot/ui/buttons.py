"""Кнопки. Цвет несёт смысл, поэтому его выбирают только через эти функции.

- success (зелёная): оплатить, подтвердить, продлить;
- danger (красная): удалить, заблокировать, отказаться;
- primary (синяя): главное действие экрана, не больше одной на экран;
- остальные без цвета.

Иконка идёт отдельным полем `icon_custom_emoji_id` и не дублируется в тексте кнопки; если
кастомные эмодзи недоступны, перед текстом ставится обычный эмодзи (см. emoji.EmojiRegistry.button).
"""

from collections.abc import Sequence
from typing import Final

from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

from plugbackup_bot import texts_ru as T
from plugbackup_bot.emoji import E, Icon

PRIMARY: Final = "primary"
SUCCESS: Final = "success"
DANGER: Final = "danger"

HOME_DATA: Final = "m:home"
# Предел Telegram для текста, который копирует кнопка.
MAX_COPY_LEN: Final = 256

Row = list[InlineKeyboardButton]


def button(
    text: str,
    *,
    data: str | None = None,
    url: str | None = None,
    copy: str | None = None,
    icon: Icon | None = None,
    style: str | None = None,
) -> InlineKeyboardButton:
    """Кнопка ровно с одним действием: callback, ссылка или копирование."""
    if sum(value is not None for value in (data, url, copy)) != 1:
        raise ValueError("у кнопки должно быть ровно одно действие: data, url или copy")
    fields: dict[str, object] = {}
    if data is not None:
        if len(data.encode()) > 64:
            raise ValueError(f"callback_data длиннее 64 байт: {data!r}")
        fields["callback_data"] = data
    if url is not None:
        fields["url"] = url
    if copy is not None:
        if not 1 <= len(copy) <= MAX_COPY_LEN:
            raise ValueError(f"текст для копирования: от 1 до {MAX_COPY_LEN} знаков")
        fields["copy_text"] = CopyTextButton(text=copy)
    if style is not None:
        fields["style"] = style
    return E.button(text, icon=icon, **fields)


def plain(text: str, data: str, icon: Icon | None = None) -> InlineKeyboardButton:
    return button(text, data=data, icon=icon)


def primary(text: str, data: str, icon: Icon | None = None) -> InlineKeyboardButton:
    return button(text, data=data, icon=icon, style=PRIMARY)


def success(text: str, data: str, icon: Icon | None = None) -> InlineKeyboardButton:
    return button(text, data=data, icon=icon, style=SUCCESS)


def danger(text: str, data: str, icon: Icon | None = None) -> InlineKeyboardButton:
    return button(text, data=data, icon=icon, style=DANGER)


def link(
    text: str, url: str, icon: Icon | None = None, style: str | None = None
) -> InlineKeyboardButton:
    return button(text, url=url, icon=icon, style=style)


def copy_button(text: str, value: str, icon: Icon | None = None) -> InlineKeyboardButton:
    """Копирует значение в буфер по нажатию, бот при этом ничего не получает."""
    return button(text, copy=value, icon=icon)


def home() -> InlineKeyboardButton:
    return plain(T.BTN_HOME, HOME_DATA, E.home)


def back(data: str) -> InlineKeyboardButton:
    return plain(T.BTN_BACK, data, E.back)


def nav(back_to: str | None = None, *, with_home: bool = True) -> Row:
    """Строка «Назад | Главное меню». Без «Назад» остаётся только главное меню."""
    row: Row = []
    if back_to is not None:
        row.append(back(back_to))
    if with_home:
        row.append(home())
    return row


def markup(*rows: Sequence[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    """Клавиатура из строк, пустые строки пропускаются."""
    return InlineKeyboardMarkup(inline_keyboard=[list(row) for row in rows if row])
