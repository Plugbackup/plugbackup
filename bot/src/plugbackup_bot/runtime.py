"""Сборка всех частей бота в работающую систему."""

import asyncio
import logging
import os
import stat
import time
from collections.abc import Collection
from dataclasses import dataclass, field

from aiogram import Bot, Dispatcher
from aiogram.types import ErrorEvent

from plugbackup_bot.config import Settings
from plugbackup_bot.db import Database, migrate
from plugbackup_bot.db.nodes import sync_nodes
from plugbackup_bot.domain.plans import PlanCatalog, PlanConfigError, load_catalog
from plugbackup_bot.infra.clock import Clock, SystemClock
from plugbackup_bot.infra.storectl import SshStorectl, Storectl
from plugbackup_bot.scheduler import Scheduler, Task
from plugbackup_bot.services.access import AccessService
from plugbackup_bot.services.admin import AdminService
from plugbackup_bot.services.alert_settings import AlertSettingsService
from plugbackup_bot.services.alerts import AlertEngine
from plugbackup_bot.services.billing import BillingService
from plugbackup_bot.services.broadcast import BroadcastService
from plugbackup_bot.services.health import HealthMonitor, Probe, tcp_probe
from plugbackup_bot.services.lifecycle import LifecycleService
from plugbackup_bot.services.maintenance import prune_all
from plugbackup_bot.services.notify import TelegramNotifier
from plugbackup_bot.services.provisioning import Provisioner
from plugbackup_bot.services.restore import RestoreService
from plugbackup_bot.services.usage import UsagePoller
from plugbackup_bot.tg import texts
from plugbackup_bot.tg.alert_texts import TelegramAlertRenderer
from plugbackup_bot.tg.app import Deps, build_dispatcher
from plugbackup_bot.tg.hooks import ClientHooks
from plugbackup_bot.tg.notices import TelegramNoticeRenderer
from plugbackup_bot.tg.stars import TelegramStars

log = logging.getLogger(__name__)

# Интервалы задач, секунды. Платежи и ключи обрабатываются сразу в handlers,
# планировщик нужен для повторов и всего, что зависит от времени.
PROVISIONING_EVERY_S = 30
LIFECYCLE_EVERY_S = 60
# После старта даём накопившимся платежам дойти до базы, прежде чем кого-то блокировать.
LIFECYCLE_STARTUP_DELAY_S = 300
USAGE_EVERY_S = 3600
USAGE_STARTUP_DELAY_S = 60
SERVICE_EVENTS_EVERY_S = 120
DIGEST_EVERY_S = 900
HEALTH_EVERY_S = 120
MAINTENANCE_EVERY_S = 24 * 3600
MAINTENANCE_STARTUP_DELAY_S = 600

# Не чаще раза в 5 минут сообщаем админу о необработанных ошибках в handlers.
ERROR_ALERT_GAP_S = 300

COMMANDS = (
    ("start", "о сервисе"),
    ("status", "занято, тариф, дата списания"),
    ("upgrade", "сменить тариф"),
    ("key", "заменить SSH-ключ"),
    ("alerts", "настройки уведомлений"),
    ("restore", "запросить восстановление"),
    ("rules", "правила"),
    ("help", "справка"),
)

# Меню администратора: те же команды клиента плюс админские. Показывается только админам.
ADMIN_COMMANDS = (
    ("admin", "панель администратора"),
    ("stats", "статистика и выручка"),
    ("nodes", "ноды и их загрузка"),
    ("invite", "инвайт-коды беты"),
    ("client", "карточка клиента"),
    ("broadcast", "рассылка"),
    *COMMANDS,
)


@dataclass
class Runtime:
    settings: Settings
    db: Database
    bot: Bot
    dp: Dispatcher
    scheduler: Scheduler
    notifier: TelegramNotifier
    provisioner: Provisioner
    background: set[asyncio.Task[object]] = field(default_factory=set)

    async def close(self) -> None:
        for task in list(self.background):
            task.cancel()
        await self.db.close()


def check_environment(settings: Settings) -> list[str]:
    """Проблемы, которые видно до запуска: недостающие файлы, слишком открытый ключ."""
    problems: list[str] = []
    key = settings.ssh_key_path
    if not key.is_file():
        problems.append(f"нет файла SSH-ключа: {key}")
    elif os.name == "posix" and key.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        problems.append(f"права на {key} слишком широкие, нужно 0600")
    if not settings.ssh_known_hosts_path.is_file():
        problems.append(f"нет файла known_hosts: {settings.ssh_known_hosts_path}")
    try:
        load_catalog(settings.plans_file, settings.prices_stars)
    except PlanConfigError as error:
        problems.append(str(error))
    return problems


async def build_runtime(
    settings: Settings,
    bot: Bot,
    *,
    clock: Clock | None = None,
    storectl: Storectl | None = None,
    probe: Probe | None = None,
) -> Runtime:
    clock = clock or SystemClock()
    catalog: PlanCatalog = load_catalog(settings.plans_file, settings.prices_stars)
    tz = settings.default_tz

    db = await Database.open(settings.db_path)
    await migrate(db)
    async with db.transaction() as conn:
        await sync_nodes(conn, settings.nodes)

    storectl = storectl or SshStorectl(
        user=settings.ssh_user,
        key_path=settings.ssh_key_path,
        known_hosts_path=settings.ssh_known_hosts_path,
        port=settings.ssh_port,
        timeout_s=settings.storectl_timeout_s,
    )
    notifier = TelegramNotifier(bot, db, settings.admin_chat_id)
    risk_version = texts.RISK_TEXT_VERSION

    billing = BillingService(db, clock, catalog, TelegramStars(bot), risk_version)
    access = AccessService(db, clock, catalog, billing, storectl, risk_version)
    provisioner = Provisioner(db, storectl, clock, ClientHooks(db, notifier, notifier))
    lifecycle = LifecycleService(db, clock, catalog, billing, notifier, TelegramNoticeRenderer(tz))
    poller = UsagePoller(db, clock, storectl, notifier)
    alert_engine = AlertEngine(db, clock, storectl, notifier, TelegramAlertRenderer(), tz)
    health = HealthMonitor(db, clock, notifier, probe or tcp_probe(settings.ssh_port))

    background: set[asyncio.Task[object]] = set()

    def kick_provisioning() -> None:
        task = asyncio.get_running_loop().create_task(provisioner.run_due())
        background.add(task)
        task.add_done_callback(background.discard)

    deps = Deps(
        db=db,
        clock=clock,
        catalog=catalog,
        access=access,
        billing=billing,
        lifecycle=lifecycle,
        alert_settings=AlertSettingsService(db, clock, tz),
        restore=RestoreService(db, clock, tz),
        admin=AdminService(db, clock, billing),
        broadcast=BroadcastService(db, clock, notifier),
        admin_ids=settings.admin_ids,
        admin_chat_id=settings.admin_chat_id,
        messenger=notifier,
        alerter=notifier,
        default_tz=tz,
        kick_provisioning=kick_provisioning,
    )
    dp = build_dispatcher(deps)
    dp.errors.register(_error_handler(notifier))

    async def usage_cycle() -> None:
        await poller.poll()
        await alert_engine.evaluate_all()

    scheduler = Scheduler(
        [
            Task("provisioning", PROVISIONING_EVERY_S, provisioner.run_due),
            Task("lifecycle", LIFECYCLE_EVERY_S, lifecycle.tick, LIFECYCLE_STARTUP_DELAY_S),
            Task("usage", USAGE_EVERY_S, usage_cycle, USAGE_STARTUP_DELAY_S),
            Task("service_events", SERVICE_EVENTS_EVERY_S, alert_engine.process_service_events),
            Task("weekly_digest", DIGEST_EVERY_S, alert_engine.weekly_digest),
            Task("health", HEALTH_EVERY_S, health.check),
            Task(
                "maintenance",
                MAINTENANCE_EVERY_S,
                lambda: prune_all(db, clock),
                MAINTENANCE_STARTUP_DELAY_S,
            ),
        ]
    )
    return Runtime(settings, db, bot, dp, scheduler, notifier, provisioner, background)


def _error_handler(notifier: TelegramNotifier):  # type: ignore[no-untyped-def]
    last_alert: float | None = None

    async def on_error(event: ErrorEvent) -> bool:
        nonlocal last_alert
        log.error("unhandled error in a handler", exc_info=event.exception)
        now = time.monotonic()
        if last_alert is None or now - last_alert >= ERROR_ALERT_GAP_S:
            last_alert = now
            # В админ-чат уходит только тип ошибки: текст исключения может содержать данные.
            name = type(event.exception).__name__
            await notifier.alert(
                f"Необработанная ошибка в обработчике: {name}. Подробности в журнале."
            )
        return True

    return on_error


async def configure_bot(bot: Bot, admin_ids: Collection[int] = ()) -> None:
    """Меню команд в Telegram: общее и расширенное для админов. Сбой не мешает запуску."""
    from aiogram.types import BotCommand, BotCommandScopeChat

    try:
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])
    except Exception:
        log.warning("could not set the bot commands", exc_info=True)
    for admin_id in admin_ids:
        try:
            await bot.set_my_commands(
                [BotCommand(command=c, description=d) for c, d in ADMIN_COMMANDS],
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception:
            # Пока админ не написал боту, у Telegram нет такого чата: меню появится после /start.
            log.warning("could not set the admin commands for %s", admin_id, exc_info=True)
