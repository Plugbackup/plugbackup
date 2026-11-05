"""Единый вид карточек: заголовок жирным, данные в <code>, длинное в раскрываемой цитате."""

from collections.abc import Iterable

from plugbackup_bot.emoji import Icon
from plugbackup_bot.ui.markup import esc


def title(text: str, icon: Icon | None = None) -> str:
    head = f"<b>{esc(text)}</b>"
    return f"{icon} {head}" if icon is not None else head


def field(label: str, value: object) -> str:
    """«Логин: u1001», значение в <code>: по нажатию копируется."""
    return f"{esc(label)}: <code>{esc(value)}</code>"


def plain_field(label: str, value: object) -> str:
    """Поле без моноширинного шрифта, для обычных слов и дат."""
    return f"{esc(label)}: {esc(value)}"


def lines(*rows: str | None) -> str:
    return "\n".join(row for row in rows if row)


def bullets(items: Iterable[str]) -> str:
    return "\n".join(f"• {item}" for item in items)


def quote(body: str) -> str:
    """Длинное пояснение: в чате свёрнуто до нескольких строк, раскрывается по нажатию."""
    return f"<blockquote expandable>{body}</blockquote>"


def card(heading: str, *blocks: str | None, icon: Icon | None = None) -> str:
    """Заголовок и блоки через пустую строку. Пустые блоки пропускаются."""
    return "\n\n".join([title(heading, icon), *(block for block in blocks if block)])
