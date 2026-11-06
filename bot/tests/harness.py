"""Стенд для сценариев бота: настоящий Dispatcher, поддельные Telegram и storectl."""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, DeleteMessage
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, Update, User

from fakes import (
    FakeClock,
    FakeSession,
    FakeStars,
    FakeStorectl,
    Output,
    RecordingAlerter,
    RecordingMessenger,
    make_bot,
)
from helpers import CATALOG, add_node
from plugbackup_bot.db import Database
from plugbackup_bot.db import invites as invites_repo
from plugbackup_bot.domain.plans import PlanCatalog
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
from plugbackup_bot.services.payments_admin import PaymentsAdmin
from plugbackup_bot.services.plans import PlanService
from plugbackup_bot.services.provisioning import Provisioner
from plugbackup_bot.services.restore import RestoreService
from plugbackup_bot.services.support import SupportService
from plugbackup_bot.tg import texts
from plugbackup_bot.tg.app import Deps, build_dispatcher
from plugbackup_bot.tg.hooks import ClientHooks
from plugbackup_bot.tg.maint_banner import MaintenanceBanner
from plugbackup_bot.tg.notices import TelegramNoticeRenderer
from plugbackup_bot.ui.banners import BannerStore
from plugbackup_bot.ui.effects import Effects
from plugbackup_bot.ui.screen import Ui

USER = User(id=5, is_bot=False, first_name="Тест")
OTHER = User(id=6, is_bot=False, first_name="Другой")
ADMIN = User(id=900, is_bot=False, first_name="Админ")
SUPPORT = User(id=901, is_bot=False, first_name="Помощник")
ADMIN_CHAT = 777


class StepClock:
    """Монотонные часы, которые сами идут вперёд: корзина токенов успевает наполняться."""

    def __init__(self, step: float = 10.0) -> None:
        self._now = 0.0
        self._step = step

    def __call__(self) -> float:
        self._now += self._step
        return self._now


@dataclass
class Harness:
    db: Database
    clock: FakeClock
    bot: Bot
    session: FakeSession
    dp: Dispatcher
    storage: MemoryStorage
    stars: FakeStars
    storectl: FakeStorectl
    messenger: RecordingMessenger
    alerter: RecordingAlerter
    billing: BillingService
    access: AccessService
    provisioner: Provisioner
    lifecycle: LifecycleService
    alert_settings: AlertSettingsService
    restore: RestoreService
    admin: AdminService
    directory: AdminDirectory
    catalog: PlanCatalog
    plans: PlanService
    invites: InviteService
    nodes: NodeService
    broadcast: BroadcastService
    maintenance: MaintenanceMode
    support: SupportService
    payments: PaymentsAdmin
    exports: ExportService
    kicks: list[int] = field(default_factory=list)
    broadcast_kicks: list[int] = field(default_factory=list)
    # Отвечает ли нода на проверку связи: тест переключает через set_node_up.
    _probe_state: dict[str, bool] = field(default_factory=dict)
    _update_id: int = 0

    @classmethod
    async def build(
        cls,
        db: Database,
        clock: FakeClock,
        *,
        monotonic: Callable[[], float] | None = None,
        node_capacity_gb: int = 1000,
        assets_dir: Path | None = None,
    ) -> "Harness":
        await add_node(db, capacity_gb=node_capacity_gb)
        bot, session = make_bot()
        stars, storectl = FakeStars(), FakeStorectl()
        messenger, alerter = RecordingMessenger(), RecordingAlerter()
        # У каждого стенда свой каталог тарифов: правки админа не просачиваются в другие тесты.
        catalog = PlanCatalog(CATALOG.plans)
        billing = BillingService(db, clock, catalog, stars, texts.RISK_TEXT_VERSION)
        access = AccessService(db, clock, catalog, billing, storectl, texts.RISK_TEXT_VERSION)
        provisioner = Provisioner(db, storectl, clock, ClientHooks(db, messenger, alerter))
        lifecycle = LifecycleService(
            db, clock, catalog, billing, messenger, TelegramNoticeRenderer("Europe/Moscow")
        )
        alert_settings = AlertSettingsService(db, clock, "Europe/Moscow")
        restore = RestoreService(db, clock, "Europe/Moscow")
        admin = AdminService(db, clock, billing, catalog)
        plans = PlanService(db, clock, catalog)
        await plans.seed(catalog.plans)
        await plans.load()
        invites = InviteService(db, clock)
        directory = AdminDirectory(db, clock, [ADMIN.id])
        await directory.sync()
        broadcast = BroadcastService(db, clock, messenger, catalog, rate_per_s=1000)
        maintenance = MaintenanceMode(db, clock, broadcast, directory)
        support = SupportService(db, clock)
        payments = PaymentsAdmin(db, clock, stars, billing, admin)
        exports = ExportService(db, clock, "Europe/Moscow")
        storage = MemoryStorage()
        state = {"up": True}

        async def probe(node: object) -> bool:
            return state["up"]

        nodes = NodeService(db, clock, admin, probe)
        harness = cls(
            db, clock, bot, session, None, storage,  # type: ignore[arg-type]
            stars, storectl, messenger, alerter, billing, access, provisioner, lifecycle,
            alert_settings, restore, admin, directory, catalog, plans, invites, nodes, broadcast,
            maintenance, support, payments, exports,
        )  # fmt: skip
        harness._probe_state = state
        deps = Deps(
            db=db,
            clock=clock,
            catalog=catalog,
            access=access,
            billing=billing,
            lifecycle=lifecycle,
            alert_settings=alert_settings,
            restore=restore,
            admin=admin,
            broadcast=broadcast,
            directory=directory,
            plans=plans,
            invites=invites,
            nodes=nodes,
            maintenance=maintenance,
            support=support,
            payments=payments,
            exports=exports,
            admin_chat_id=ADMIN_CHAT,
            messenger=messenger,
            alerter=alerter,
            default_tz="Europe/Moscow",
            kick_provisioning=lambda: harness.kicks.append(1),
            kick_broadcasts=lambda: harness.broadcast_kicks.append(1),
            ui=Ui(
                BannerStore(db, assets_dir, clock) if assets_dir else None,
                Effects(),
                MaintenanceBanner(maintenance),
            ),
        )
        harness.dp = build_dispatcher(deps, storage=storage, monotonic=monotonic or StepClock())
        return harness

    @property
    def node_is_up(self) -> bool:
        return self._probe_state["up"]

    def set_node_up(self, up: bool) -> None:
        self._probe_state["up"] = up

    # --- входящие события ---

    async def _feed(self, **fields: Any) -> None:
        self._update_id += 1
        await self.dp.feed_update(self.bot, Update(update_id=self._update_id, **fields))

    def _message(self, text: str, user: User, chat_type: str = "private") -> Message:
        return Message(
            message_id=self._update_id + 1000,
            date=datetime.now(UTC),
            chat=Chat(id=user.id if chat_type == "private" else -100, type=chat_type),
            from_user=user,
            text=text,
        )

    async def say(self, text: str, user: User = USER, chat_type: str = "private") -> None:
        await self._feed(message=self._message(text, user, chat_type))

    async def press(self, data: str, user: User = USER, kind: str | None = None) -> None:
        """Нажатие кнопки. Сообщение с кнопкой — последнее сообщение бота в этом чате
        (kind задаёт его вид: «text» или «photo», по умолчанию как было на самом деле)."""
        message_id, last_kind = self.session.last_message.get(
            user.id, (self._update_id + 1000, "text")
        )
        kind = kind or last_kind
        self.session.message_kinds[(user.id, message_id)] = kind
        source = self._message("меню", user)
        updates: dict[str, Any] = {"message_id": message_id}
        if kind == "photo":
            updates.update(
                text=None,
                caption="меню",
                photo=[PhotoSize(file_id="f", file_unique_id="u", width=1, height=1)],
            )
        source = source.model_copy(update=updates)
        query = CallbackQuery(
            id=str(self._update_id + 1),
            from_user=user,
            chat_instance="test",
            data=data,
            message=source,
        )
        await self._feed(callback_query=query)

    # --- состояние и ответы ---

    async def fsm_state(self, user: User = USER) -> str | None:
        key = StorageKey(bot_id=self.bot.id, chat_id=user.id, user_id=user.id)
        return await self.storage.get_state(key)

    def texts(self) -> list[str]:
        """Тексты всего показанного: и новых сообщений, и правок."""
        return [output.text for output in self.session.outputs()]

    def last(self) -> Output:
        return self.session.outputs()[-1]

    def last_buttons(
        self, *, nav: bool = False, icons: bool = False
    ) -> list[tuple[str, str | None, str | None]]:
        """Кнопки последнего сообщения: (текст, callback_data, url).

        Без nav кнопки «Назад» и «Главное меню» не попадают в список, без icons у текста
        кнопки убран обычный эмодзи-значок в начале.
        """
        markup = self.last().reply_markup
        if markup is None:
            return []
        found = []
        for row in markup.inline_keyboard:  # type: ignore[union-attr]
            for button in row:
                text = button.text if icons else re.sub(r"^[^\w]+", "", button.text)
                if not nav and (button.callback_data == "m:home" or text == "Назад"):
                    continue
                found.append((text, button.callback_data, button.url))
        return found

    def callback_answers(self) -> list[AnswerCallbackQuery]:
        return self.session.of(AnswerCallbackQuery)

    async def drain_tasks(self) -> None:
        import asyncio

        tasks = list(self.dp.workflow_data["background_tasks"])
        if tasks:
            await asyncio.gather(*tasks)

    def deleted(self) -> list[DeleteMessage]:
        return self.session.of(DeleteMessage)

    # --- подготовка ---

    async def invite(self) -> str:
        async with self.db.transaction() as conn:
            [code] = await invites_repo.create_invites(conn, 1, 1, self.clock.now())
        return code
