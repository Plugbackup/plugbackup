"""Сборка диспетчера aiogram: middleware, роутеры и зависимости handlers."""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from aiogram import Dispatcher
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage

from plugbackup_bot.db import Database
from plugbackup_bot.domain.plans import PlanCatalog
from plugbackup_bot.infra.clock import Clock
from plugbackup_bot.services.access import AccessService
from plugbackup_bot.services.admin import AdminService
from plugbackup_bot.services.admins import AdminDirectory
from plugbackup_bot.services.alert_settings import AlertSettingsService
from plugbackup_bot.services.billing import BillingService
from plugbackup_bot.services.broadcast import BroadcastService
from plugbackup_bot.services.exports import ExportService
from plugbackup_bot.services.invites import InviteService
from plugbackup_bot.services.lifecycle import LifecycleService
from plugbackup_bot.services.maintenance_mode import MaintenanceMode
from plugbackup_bot.services.nodes import NodeService
from plugbackup_bot.services.notify import AdminAlerter, Messenger
from plugbackup_bot.services.payments_admin import PaymentsAdmin
from plugbackup_bot.services.plans import PlanService
from plugbackup_bot.services.restore import RestoreService
from plugbackup_bot.services.support import SupportService
from plugbackup_bot.tg.admin.router import build_router as build_admin_router
from plugbackup_bot.tg.handlers import (
    access,
    alerts,
    emoji_preview,
    lifecycle,
    payments,
    restore,
    start,
    status,
    support,
)
from plugbackup_bot.tg.middlewares import AttemptLimiter, ThrottleMiddleware, UserMiddleware
from plugbackup_bot.ui.screen import Ui


@dataclass
class Deps:
    db: Database
    clock: Clock
    catalog: PlanCatalog
    access: AccessService
    billing: BillingService
    lifecycle: LifecycleService
    alert_settings: AlertSettingsService
    restore: RestoreService
    admin: AdminService
    broadcast: BroadcastService
    directory: AdminDirectory
    plans: PlanService
    invites: InviteService
    nodes: NodeService
    maintenance: MaintenanceMode
    support: SupportService
    payments: PaymentsAdmin
    exports: ExportService
    admin_chat_id: int
    messenger: Messenger
    alerter: AdminAlerter
    default_tz: str
    # Просит исполнителя очереди сразу взяться за новые задачи, не дожидаясь планировщика.
    kick_provisioning: Callable[[], None] = lambda: None
    # То же для рассылок: свежесозданная рассылка уходит сразу, а не по таймеру.
    kick_broadcasts: Callable[[], None] = lambda: None
    emoji_pack_file: Path = Path("emoji_pack.json")
    # Показ экранов: правка текущего сообщения, баннеры, эффекты.
    ui: Ui = field(default_factory=Ui)


def build_dispatcher(
    deps: Deps,
    *,
    storage: BaseStorage | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> Dispatcher:
    dp = Dispatcher(
        storage=storage or MemoryStorage(),
        db=deps.db,
        catalog=deps.catalog,
        access=deps.access,
        billing=deps.billing,
        lifecycle=deps.lifecycle,
        alert_settings=deps.alert_settings,
        restore=deps.restore,
        admin=deps.admin,
        directory=deps.directory,
        plans=deps.plans,
        invites=deps.invites,
        nodes=deps.nodes,
        broadcast=deps.broadcast,
        maintenance=deps.maintenance,
        support=deps.support,
        payments=deps.payments,
        exports=deps.exports,
        admin_chat_id=deps.admin_chat_id,
        clock=deps.clock,
        background_tasks=set[asyncio.Task[None]](),
        messenger=deps.messenger,
        alerter=deps.alerter,
        default_tz=deps.default_tz,
        kick_provisioning=deps.kick_provisioning,
        kick_broadcasts=deps.kick_broadcasts,
        emoji_pack_file=deps.emoji_pack_file,
        ui=deps.ui,
        invite_limiter=AttemptLimiter(now=monotonic),
    )
    # Сначала ограничение частоты, потом учёт пользователя.
    for observer in (dp.message, dp.callback_query):
        observer.outer_middleware(ThrottleMiddleware(now=monotonic))
        observer.outer_middleware(UserMiddleware(deps.db, deps.clock))
    dp.include_router(payments.build_router())
    dp.include_router(start.build_router())
    dp.include_router(status.build_router())
    dp.include_router(lifecycle.build_router())
    dp.include_router(alerts.build_router())
    dp.include_router(restore.build_router())
    dp.include_router(support.build_router())
    # Админка раньше «любого текста»: иначе текст рассылки уйдёт в запасной handler.
    dp.include_router(build_admin_router(deps.directory))
    dp.include_router(emoji_preview.build_router(deps.directory))
    # Последним: внутри него handler «любой текст».
    dp.include_router(access.build_router())
    return dp
