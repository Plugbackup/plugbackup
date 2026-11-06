"""Общий режим техработ: плашка во всех ответах бота и рассылка в начале и конце.

Состояние лежит в app_settings и переживает перезапуск. Администраторы плашку не видят: им нужно
работать с ботом, пока клиенты ждут. Плановые работы на одной ноде (services.nodes) устроены
отдельно и режима не включают.
"""

import json
import logging
from dataclasses import dataclass
from enum import StrEnum

from plugbackup_bot import texts_ru as T
from plugbackup_bot.db import Database, audit
from plugbackup_bot.db import settings as settings_repo
from plugbackup_bot.domain.roles import Actor, Permission
from plugbackup_bot.infra.clock import Clock
from plugbackup_bot.services.admins import AdminDirectory
from plugbackup_bot.services.broadcast import BroadcastService, Segment

log = logging.getLogger(__name__)

SETTING_KEY = "maintenance"
MAX_TEXT = 200
KIND = "maintenance"


class ModeKind(StrEnum):
    OK = "ok"
    ALREADY = "already"
    INVALID = "invalid"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class MaintenanceState:
    text: str
    since: int
    by: int


def _decode(raw: str | None) -> MaintenanceState | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return MaintenanceState(str(data["text"]), int(data["since"]), int(data["by"]))
    except (ValueError, TypeError, KeyError):
        log.warning("maintenance setting is damaged, treating the mode as off")
        return None


class MaintenanceMode:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        broadcasts: BroadcastService,
        directory: AdminDirectory,
    ) -> None:
        self._db = db
        self._clock = clock
        self._broadcasts = broadcasts
        self._directory = directory
        self._state: MaintenanceState | None = None

    async def load(self) -> None:
        """При старте бота: подхватывает режим, включённый до перезапуска."""
        async with self._db.read() as conn:
            self._state = _decode(await settings_repo.get(conn, SETTING_KEY))

    @property
    def state(self) -> MaintenanceState | None:
        return self._state

    @property
    def active(self) -> bool:
        return self._state is not None

    async def text_for(self, tg_id: int) -> str | None:
        """Текст плашки для этого пользователя или None: режим выключен либо это админ."""
        state = self._state
        if state is None:
            return None
        if await self._directory.actor_for(tg_id) is not None:
            return None
        return state.text

    async def start(self, actor: Actor, text: str) -> ModeKind:
        if not actor.can(Permission.MAINTENANCE):
            return ModeKind.DENIED
        text = " ".join(text.split())
        if not 1 <= len(text) <= MAX_TEXT:
            return ModeKind.INVALID
        if self._state is not None:
            return ModeKind.ALREADY
        now = self._clock.now()
        state = MaintenanceState(text, now, actor.tg_id)
        async with self._db.transaction() as conn:
            await settings_repo.put(
                conn,
                SETTING_KEY,
                json.dumps({"text": state.text, "since": state.since, "by": state.by}),
                now,
            )
            await audit.record(conn, actor, "maintenance.start", now=now, length=len(text))
        self._state = state
        await self._broadcasts.create(
            actor, segment=Segment.ALL, text=T.MAINT_BROADCAST_START.format(text=text), kind=KIND
        )
        return ModeKind.OK

    async def end(self, actor: Actor) -> ModeKind:
        if not actor.can(Permission.MAINTENANCE):
            return ModeKind.DENIED
        if self._state is None:
            return ModeKind.ALREADY
        now = self._clock.now()
        async with self._db.transaction() as conn:
            await settings_repo.delete(conn, SETTING_KEY)
            await audit.record(conn, actor, "maintenance.end", now=now)
        self._state = None
        await self._broadcasts.create(
            actor, segment=Segment.ALL, text=T.MAINT_BROADCAST_END, kind=KIND
        )
        return ModeKind.OK
