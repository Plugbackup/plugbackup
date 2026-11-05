"""/admin: меню разделов по правам роли, отмена ввода и устаревшие кнопки."""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from plugbackup_bot import texts_ru as T
from plugbackup_bot.domain.roles import Actor, Permission
from plugbackup_bot.emoji import E, Icon
from plugbackup_bot.tg.admin import core
from plugbackup_bot.tg.admin.core import Adm, admin_nav, cb, requires
from plugbackup_bot.ui import buttons, cards, pagination
from plugbackup_bot.ui.markup import fill
from plugbackup_bot.ui.screen import Screen, Ui

# Разделы меню: (право, подпись, раздел, значок). Кнопка видна, только если право есть у роли.
SECTIONS: tuple[tuple[Permission, str, str, Icon], ...] = (
    (Permission.DASHBOARD, T.BTN_ADM_DASHBOARD, core.DASH, E.chart),
    (Permission.CLIENTS_VIEW, T.BTN_ADM_CLIENTS, core.CLIENTS, E.user),
)


def menu_screen(actor: Actor, badges: dict[str, int] | None = None) -> Screen:
    """Меню админки. badges: число необработанного по разделу («Заявки (2)»)."""
    badges = badges or {}
    rows = []
    pair: list = []
    for permission, title, section, icon in SECTIONS:
        if not actor.can(permission):
            continue
        count = badges.get(section, 0)
        label = T.ADM_BADGE.format(title=title, count=count) if count else title
        pair.append(buttons.plain(label, cb(section), icon))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([buttons.plain(T.BTN_ADM_CLIENT_MENU, buttons.HOME_DATA, E.home)])
    text = cards.card(
        T.ADM_MENU_TITLE,
        cards.lines(
            fill(T.ADM_MENU_ROLE, role=T.ROLE_NAMES[actor.role.value]),
            T.ADM_MENU_HINT,
        ),
        icon=E.admin,
    )
    return Screen(text, buttons.markup(*rows))


@requires(Permission.PANEL)
async def cmd_admin(message: Message, state: FSMContext, ui: Ui, actor: Actor) -> None:
    await state.clear()
    await ui.show(message, menu_screen(actor))


@requires(Permission.PANEL)
async def cb_menu(callback: CallbackQuery, state: FSMContext, ui: Ui, actor: Actor) -> None:
    await state.clear()
    await callback.answer()
    await ui.show(callback, menu_screen(actor))


@requires(Permission.PANEL)
async def cmd_cancel(message: Message, state: FSMContext, ui: Ui, actor: Actor) -> None:
    await state.clear()
    await ui.show(message, Screen(T.ADM_CANCELLED, buttons.markup(admin_nav())))


@requires(Permission.PANEL)
async def cb_stale(callback: CallbackQuery, actor: Actor) -> None:
    """Кнопка из старого сообщения: разбор callback_data не удался."""
    await callback.answer(T.ADM_STALE, show_alert=True)


@requires(Permission.PANEL)
async def cb_noop(callback: CallbackQuery, actor: Actor) -> None:
    """Номер страницы между стрелками: нажатие ничего не делает."""
    await callback.answer()


def register(router: Router) -> None:
    router.callback_query.register(cb_noop, F.data == pagination.NOOP_DATA)
    router.message.register(cmd_admin, Command("admin"))
    router.message.register(cmd_cancel, Command("cancel"))
    router.callback_query.register(cb_menu, Adm.filter(F.s == core.MENU))


def register_fallback(router: Router) -> None:
    """Регистрируется последним: ловит всё админское, что не разобрали остальные."""
    router.callback_query.register(cb_stale, F.data.startswith("a:"))
