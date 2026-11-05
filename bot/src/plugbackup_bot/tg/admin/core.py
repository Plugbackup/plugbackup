"""Основа админки: callback_data, доступ по ролям, подтверждение опасных действий.

Права проверяются на сервере при каждом нажатии и каждом сообщении: у любого обработчика
админки есть декоратор `requires(право)`, а тест обходит все зарегистрированные обработчики и
проверяет, что ни один не остался без права. Скрытие кнопок в интерфейсе защитой не считается.
"""

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Final

from aiogram.filters import Filter
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, TelegramObject

from plugbackup_bot import texts_ru as T
from plugbackup_bot.domain.roles import Actor, Permission
from plugbackup_bot.emoji import E
from plugbackup_bot.services.admins import AdminDirectory
from plugbackup_bot.ui import buttons, cards, pagination
from plugbackup_bot.ui.pagination import Page
from plugbackup_bot.ui.screen import Screen

log = logging.getLogger(__name__)

# Разделы. Короткие коды: callback_data не длиннее 64 байт.
MENU: Final = "m"
DASH: Final = "d"
CLIENTS: Final = "c"
TARIFFS: Final = "t"
INVITES: Final = "v"
BROADCASTS: Final = "b"
NODES: Final = "n"
MAINT: Final = "w"
SUPPORT: Final = "s"
RESTORE: Final = "r"
PAYMENTS: Final = "p"
ADMINS: Final = "u"
JOURNAL: Final = "j"
EXPORT: Final = "e"


class Adm(CallbackData, prefix="a"):
    """callback_data админки: раздел, действие и до трёх аргументов.

    Разбор проверяет типы, поэтому мусор в числовых полях до обработчика не доходит.
    """

    s: str
    a: str = ""
    i: int = 0  # id записи
    p: int = 0  # страница, число или другое значение
    x: str = ""  # короткий код (тариф, сегмент); без «:»


def cb(section: str, action: str = "", *, i: int = 0, p: int = 0, x: str = "") -> str:
    return Adm(s=section, a=action, i=i, p=p, x=x).pack()


class AdminFilter(Filter):
    """Пропускает только администраторов и отдаёт обработчику Actor с актуальной ролью.

    Не администратор не ловится этим фильтром: событие идёт дальше по роутерам, как у любого
    пользователя бота.
    """

    def __init__(self, directory: AdminDirectory) -> None:
        self._directory = directory

    async def __call__(self, event: TelegramObject) -> bool | dict[str, Any]:
        user = getattr(event, "from_user", None)
        if user is None:
            return False
        actor = await self._directory.actor_for(user.id)
        return {"actor": actor} if actor is not None else False


Handler = Callable[..., Awaitable[Any]]


def requires(permission: Permission) -> Callable[[Handler], Handler]:
    """Обработчик выполняется, только если у роли администратора есть право `permission`."""

    def decorate(handler: Handler) -> Handler:
        @functools.wraps(handler)
        async def wrapper(event: Message | CallbackQuery, *args: Any, **kwargs: Any) -> Any:
            actor: Actor = kwargs["actor"]
            if not actor.can(permission):
                log.warning(
                    "denied: admin %s (%s) tried %s",
                    actor.tg_id,
                    actor.role.value,
                    permission.value,
                )
                if isinstance(event, CallbackQuery):
                    await event.answer(T.ADM_DENIED, show_alert=True)
                else:
                    await event.answer(T.ADM_DENIED)
                return None
            return await handler(event, *args, **kwargs)

        wrapper.__permission__ = permission  # type: ignore[attr-defined]
        return wrapper

    return decorate


def permission_of(handler: Handler) -> Permission | None:
    return getattr(handler, "__permission__", None)


# --- общие экраны ---


def admin_nav(back_to: str | None = None) -> list[Any]:
    """Строка «Назад | Главное меню». Главное меню админки ведёт в /admin."""
    row = []
    if back_to is not None:
        row.append(buttons.back(back_to))
    row.append(buttons.plain(T.BTN_ADM_MENU, cb(MENU), E.admin))
    return row


def confirm_screen(body: str, *, yes: str, back: str) -> Screen:
    """Экран подтверждения опасного действия: красная кнопка и «Назад» как отмена."""
    text = cards.card(T.ADM_CONFIRM_TITLE, body, icon=E.warning)
    markup: InlineKeyboardMarkup = buttons.markup(
        [buttons.danger(T.BTN_ADM_CONFIRM, yes, E.warning)], admin_nav(back)
    )
    return Screen(text, markup)


def notice_screen(text: str, back: str) -> Screen:
    """Короткое сообщение с возвратом: результат действия или причина отказа."""
    return Screen(text, buttons.markup(admin_nav(back)))


def pager(page: Page[Any], section: str, action: str, *, i: int = 0, x: str = "") -> list[Any]:
    """Строка листания «‹ 2 из 5 ›». Пустая, если страница одна."""
    if page.pages <= 1:
        return []
    row: list[Any] = []
    if page.has_prev:
        row.append(buttons.plain(T.BTN_PAGE_PREV, cb(section, action, i=i, p=page.number - 1, x=x)))
    label = T.PAGE_LABEL.format(page=page.number + 1, pages=page.pages)
    row.append(buttons.plain(label, pagination.NOOP_DATA))
    if page.has_next:
        row.append(buttons.plain(T.BTN_PAGE_NEXT, cb(section, action, i=i, p=page.number + 1, x=x)))
    return row


def ask_screen(title: str, body: str, back: str) -> Screen:
    """Просьба прислать текст или число. Отмена: «Назад» или /cancel."""
    return Screen(
        cards.card(title, body, T.ADM_CANCEL_HINT, icon=E.settings),
        buttons.markup(admin_nav(back)),
    )
