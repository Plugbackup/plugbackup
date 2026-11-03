"""Стенд для сценариев бота: настоящий Dispatcher, поддельные Telegram и storectl."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, DeleteMessage, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from fakes import (
    FakeClock,
    FakeSession,
    FakeStars,
    FakeStorectl,
    RecordingAlerter,
    RecordingMessenger,
    make_bot,
)
from helpers import CATALOG, add_node
from plugbackup_bot.db import Database
from plugbackup_bot.db import invites as invites_repo
from plugbackup_bot.services.access import AccessService
from plugbackup_bot.services.admin import AdminService
from plugbackup_bot.services.alert_settings import AlertSettingsService
from plugbackup_bot.services.billing import BillingService
from plugbackup_bot.services.broadcast import BroadcastService
from plugbackup_bot.services.lifecycle import LifecycleService
from plugbackup_bot.services.provisioning import Provisioner
from plugbackup_bot.services.restore import RestoreService
from plugbackup_bot.tg import texts
from plugbackup_bot.tg.app import Deps, build_dispatcher
from plugbackup_bot.tg.hooks import ClientHooks
from plugbackup_bot.tg.notices import TelegramNoticeRenderer

USER = User(id=5, is_bot=False, first_name="Тест")
OTHER = User(id=6, is_bot=False, first_name="Другой")
ADMIN = User(id=900, is_bot=False, first_name="Админ")
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
    broadcast: BroadcastService
    kicks: list[int] = field(default_factory=list)
    _update_id: int = 0

    @classmethod
    async def build(
        cls,
        db: Database,
        clock: FakeClock,
        *,
        monotonic: Callable[[], float] | None = None,
        node_capacity_gb: int = 1000,
    ) -> "Harness":
        await add_node(db, capacity_gb=node_capacity_gb)
        bot, session = make_bot()
        stars, storectl = FakeStars(), FakeStorectl()
        messenger, alerter = RecordingMessenger(), RecordingAlerter()
        billing = BillingService(db, clock, CATALOG, stars, texts.RISK_TEXT_VERSION)
        access = AccessService(db, clock, CATALOG, billing, storectl, texts.RISK_TEXT_VERSION)
        provisioner = Provisioner(db, storectl, clock, ClientHooks(db, messenger, alerter))
        lifecycle = LifecycleService(
            db, clock, CATALOG, billing, messenger, TelegramNoticeRenderer("Europe/Moscow")
        )
        alert_settings = AlertSettingsService(db, clock, "Europe/Moscow")
        restore = RestoreService(db, clock, "Europe/Moscow")
        admin = AdminService(db, clock, billing)
        broadcast = BroadcastService(db, clock, messenger, rate_per_s=1000)
        storage = MemoryStorage()
        harness = cls(
            db, clock, bot, session, None, storage,  # type: ignore[arg-type]
            stars, storectl, messenger, alerter, billing, access, provisioner, lifecycle,
            alert_settings, restore, admin, broadcast,
        )  # fmt: skip
        deps = Deps(
            db=db,
            clock=clock,
            catalog=CATALOG,
            access=access,
            billing=billing,
            lifecycle=lifecycle,
            alert_settings=alert_settings,
            restore=restore,
            admin=admin,
            broadcast=broadcast,
            admin_ids=[ADMIN.id],
            admin_chat_id=ADMIN_CHAT,
            messenger=messenger,
            alerter=alerter,
            default_tz="Europe/Moscow",
            kick_provisioning=lambda: harness.kicks.append(1),
        )
        harness.dp = build_dispatcher(deps, storage=storage, monotonic=monotonic or StepClock())
        return harness

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

    async def press(self, data: str, user: User = USER) -> None:
        query = CallbackQuery(
            id=str(self._update_id + 1),
            from_user=user,
            chat_instance="test",
            data=data,
            message=self._message("меню", user),
        )
        await self._feed(callback_query=query)

    # --- состояние и ответы ---

    async def fsm_state(self, user: User = USER) -> str | None:
        key = StorageKey(bot_id=self.bot.id, chat_id=user.id, user_id=user.id)
        return await self.storage.get_state(key)

    def texts(self) -> list[str]:
        return self.session.texts()

    def last(self) -> SendMessage:
        return self.session.of(SendMessage)[-1]

    def last_buttons(self) -> list[tuple[str, str | None, str | None]]:
        """Кнопки последнего сообщения: (текст, callback_data, url)."""
        markup = self.last().reply_markup
        if markup is None:
            return []
        return [  # type: ignore[union-attr]
            (b.text, b.callback_data, b.url) for row in markup.inline_keyboard for b in row
        ]

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
