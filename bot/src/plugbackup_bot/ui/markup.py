"""Безопасная подстановка в HTML-тексты.

Бот шлёт сообщения с parse_mode=HTML. Всё, что приходит снаружи (имена, логины, текст
администратора), должно экранироваться. `fill` делает это для каждого значения сам: готовый HTML
нужно явно пометить как `Raw`.
"""

import html


class Raw(str):
    """Уже безопасный HTML: при подстановке не экранируется."""

    __slots__ = ()


def esc(value: object) -> str:
    if isinstance(value, Raw):
        return str(value)
    return html.escape(str(value))


def fill(template: str, **values: object) -> Raw:
    """Подставляет значения в шаблон из texts_ru, экранируя каждое. Результат помечен как Raw."""
    return Raw(template.format(**{name: esc(value) for name, value in values.items()}))
