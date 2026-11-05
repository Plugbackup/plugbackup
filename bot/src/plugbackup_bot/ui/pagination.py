"""Постраничный вывод длинных списков: «‹ 2 из 5 ›»."""

from collections.abc import Sequence
from dataclasses import dataclass

from aiogram.types import InlineKeyboardButton

from plugbackup_bot import texts_ru as T
from plugbackup_bot.ui import buttons

PAGE_SIZE = 8
# Кнопка с номером страницы ничего не делает: нужна, чтобы строка выглядела целой.
NOOP_DATA = "pg:x"


@dataclass(frozen=True, slots=True)
class Page[Item]:
    items: Sequence[Item]
    number: int  # с нуля
    pages: int
    total: int

    @property
    def has_prev(self) -> bool:
        return self.number > 0

    @property
    def has_next(self) -> bool:
        return self.number + 1 < self.pages

    @property
    def first_index(self) -> int:
        """Номер первой записи страницы в общем списке, с единицы."""
        return self.number * PAGE_SIZE + 1


def paginate[Item](items: Sequence[Item], number: int, size: int = PAGE_SIZE) -> Page[Item]:
    """Страница `number` (с нуля). Номер вне диапазона приводится к ближайшему."""
    pages = max(1, -(-len(items) // size))
    number = min(max(number, 0), pages - 1)
    return Page(items[number * size : (number + 1) * size], number, pages, len(items))


def pager(page: Page[object], prefix: str) -> list[InlineKeyboardButton]:
    """Строка кнопок листания. Пустая, если страница одна. callback_data: prefix + номер."""
    if page.pages <= 1:
        return []
    row: list[InlineKeyboardButton] = []
    if page.has_prev:
        row.append(buttons.plain(T.BTN_PAGE_PREV, f"{prefix}{page.number - 1}"))
    row.append(
        buttons.plain(T.PAGE_LABEL.format(page=page.number + 1, pages=page.pages), NOOP_DATA)
    )
    if page.has_next:
        row.append(buttons.plain(T.BTN_PAGE_NEXT, f"{prefix}{page.number + 1}"))
    return row


def parse_page(raw: str) -> int:
    """Номер страницы из callback_data. Мусор превращается в первую страницу."""
    return int(raw) if raw.isascii() and raw.isdecimal() and len(raw) <= 6 else 0
