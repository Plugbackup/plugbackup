"""Дашборд: клиенты по тарифам, выручка, продано против ёмкости, ноды и очереди."""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from plugbackup_bot import texts_ru as T
from plugbackup_bot.db import Database
from plugbackup_bot.domain.plans import PlanCatalog
from plugbackup_bot.domain.roles import Actor, Permission
from plugbackup_bot.emoji import E
from plugbackup_bot.infra.clock import Clock
from plugbackup_bot.services import stats as stats_service
from plugbackup_bot.services.admin import AdminService
from plugbackup_bot.services.maintenance_mode import MaintenanceMode
from plugbackup_bot.tg.admin import core, views
from plugbackup_bot.tg.admin.core import Adm, admin_nav, cb, requires
from plugbackup_bot.ui import buttons
from plugbackup_bot.ui.markup import fill
from plugbackup_bot.ui.screen import Screen, Ui


async def _screen(
    db: Database,
    clock: Clock,
    admin: AdminService,
    catalog: PlanCatalog,
    maintenance: MaintenanceMode,
    tz: str,
) -> Screen:
    data = await stats_service.collect(db, clock, admin, tz, catalog.beta.id)
    text = views.dashboard(data, catalog, tz)
    if maintenance.state is not None:
        text = f"{fill(T.DASH_MAINT, text=maintenance.state.text)}\n\n{text}"
    refresh = buttons.plain(T.BTN_ADM_REFRESH, cb(core.DASH), E.refresh)
    return Screen(text, buttons.markup([refresh], admin_nav()))


@requires(Permission.DASHBOARD)
async def cmd_stats(
    message: Message,
    ui: Ui,
    db: Database,
    clock: Clock,
    admin: AdminService,
    catalog: PlanCatalog,
    maintenance: MaintenanceMode,
    default_tz: str,
    actor: Actor,
) -> None:
    await ui.show(message, await _screen(db, clock, admin, catalog, maintenance, default_tz))


@requires(Permission.DASHBOARD)
async def cb_dashboard(
    callback: CallbackQuery,
    ui: Ui,
    db: Database,
    clock: Clock,
    admin: AdminService,
    catalog: PlanCatalog,
    maintenance: MaintenanceMode,
    default_tz: str,
    actor: Actor,
) -> None:
    await callback.answer()
    await ui.show(callback, await _screen(db, clock, admin, catalog, maintenance, default_tz))


def register(router: Router) -> None:
    router.message.register(cmd_stats, Command("stats"))
    router.callback_query.register(cb_dashboard, Adm.filter(F.s == core.DASH))
