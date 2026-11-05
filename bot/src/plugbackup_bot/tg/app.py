"""Сборка диспетчера aiogram: middleware, роутеры и зависимости handlers."""

import asyncio
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path

from aiogram import Dispatcher
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage

from plugbackup_bot.db import Database
from plugbackup_bot.domain.plans import PlanCatalog
from plugbackup_bot.infra.clock import Clock
from plugbackup_bot.services.access import AccessService
from plugbackup_bot.services.admin import AdminService
from plugbackup_bot.services.alert_settings import AlertSettingsService
from plugbackup_bot.services.billing import BillingService
from plugbackup_bot.services.broadcast import BroadcastService
from plugbackup_bot.services.lifecycle import LifecycleService
from plugbackup_bot.services.notify import AdminAlerter, Messenger
from plugbackup_bot.services.restore import RestoreService
from plugbackup_bot.tg.handlers import (
    access,
    admin,
    alerts,
    emoji_preview,
    lifecycle,
    payments,
    restore,
    start,
    status,
)
from plugbackup_bot.tg.middlewares import AttemptLimiter, ThrottleMiddleware, UserMiddleware


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
    admin_ids: Collection[int]
    admin_chat_id: int
    messenger: Messenger
    alerter: AdminAlerter
    default_tz: str
    # Просит исполнителя очереди сразу взяться за новые задачи, не дожидаясь планировщика.
    kick_provisioning: Callable[[], None] = lambda: None
    emoji_pack_file: Path = Path("emoji_pack.json")


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
        broadcast=deps.broadcast,
        admin_chat_id=deps.admin_chat_id,
        clock=deps.clock,
        background_tasks=set[asyncio.Task[None]](),
        messenger=deps.messenger,
        alerter=deps.alerter,
        default_tz=deps.default_tz,
        kick_provisioning=deps.kick_provisioning,
        emoji_pack_file=deps.emoji_pack_file,
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
    # Админка раньше «любого текста»: иначе текст рассылки уйдёт в запасной handler.
    dp.include_router(admin.build_router(deps.admin_ids))
    dp.include_router(emoji_preview.build_router(deps.admin_ids))
    # Последним: внутри него handler «любой текст».
    dp.include_router(access.build_router())
    return dp
