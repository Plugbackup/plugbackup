"""Журнал действий администраторов: кто, что и над чем. Тексты сообщений в нём не хранятся."""

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from plugbackup_bot import texts_ru as T
from plugbackup_bot.db import Database, audit
from plugbackup_bot.db.audit import AuditEntry
from plugbackup_bot.domain.roles import Actor, Permission
from plugbackup_bot.emoji import E
from plugbackup_bot.tg.admin import core
from plugbackup_bot.tg.admin.core import Adm, admin_nav, cb, pager, requires
from plugbackup_bot.tg.format import fmt_datetime
from plugbackup_bot.ui import buttons, cards
from plugbackup_bot.ui.markup import fill
from plugbackup_bot.ui.pagination import Page
from plugbackup_bot.ui.screen import Screen, Ui

J = core.JOURNAL
PAGE_SIZE = 10
# Подробности записи длиннее этого в строку не помещаются.
MAX_DETAILS = 80


def _details(entry: AuditEntry) -> str:
    if not entry.details:
        return ""
    parts = [f"{key}={value}" for key, value in sorted(entry.details.items())]
    text = ", ".join(parts)
    if len(text) > MAX_DETAILS:
        text = text[: MAX_DETAILS - 1] + "…"
    return f" ({text})"


def line(entry: AuditEntry, tz: str) -> str:
    target = (
        fill(T.JOURNAL_TARGET, type=entry.target_type, id=entry.target_id)
        if entry.target_type and entry.target_id
        else ""
    )
    return str(
        fill(
            T.JOURNAL_LINE,
            at=fmt_datetime(entry.at, tz),
            actor=entry.actor_id,
            role=entry.actor_role,
            action=T.JOURNAL_ACTIONS.get(entry.action, entry.action),
            target=target,
            details=_details(entry),
        )
    )


async def list_screen(db: Database, tz: str, number: int) -> Screen:
    async with db.read() as conn:
        total = await audit.count(conn)
        pages = max(1, -(-total // PAGE_SIZE))
        number = min(max(number, 0), pages - 1)
        entries = await audit.page(conn, limit=PAGE_SIZE, offset=number * PAGE_SIZE)
    body = (
        cards.lines(
            str(fill(T.JOURNAL_SUMMARY, total=total)),
            "",
            "\n".join(line(entry, tz) for entry in entries),
        )
        if entries
        else T.JOURNAL_EMPTY
    )
    page = Page(entries, number, pages, total)
    markup = buttons.markup(
        [buttons.plain(T.BTN_ADM_REFRESH, cb(J, "list", p=number), E.refresh)],
        pager(page, J, "list"),
        admin_nav(cb(core.MENU)),
    )
    return Screen(cards.card(T.JOURNAL_TITLE, body, icon=E.shield), markup)


@requires(Permission.JOURNAL)
async def cb_list(
    callback: CallbackQuery,
    callback_data: Adm,
    state: FSMContext,
    ui: Ui,
    db: Database,
    default_tz: str,
    actor: Actor,
) -> None:
    await state.clear()
    await callback.answer()
    await ui.show(callback, await list_screen(db, default_tz, callback_data.p))


def register(router: Router) -> None:
    router.callback_query.register(cb_list, Adm.filter((F.s == J) & F.a.in_({"", "list"})))
