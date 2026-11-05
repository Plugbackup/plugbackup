"""Администраторы и роли. Роль читается из базы при каждом действии: снятые права
перестают действовать сразу, без перезапуска бота."""

import logging
from collections.abc import Collection
from enum import StrEnum

from plugbackup_bot.db import Database, audit
from plugbackup_bot.db import admins as admins_repo
from plugbackup_bot.db.admins import AdminRow
from plugbackup_bot.domain.roles import Actor, Permission, Role
from plugbackup_bot.infra.clock import Clock

log = logging.getLogger(__name__)


class AddKind(StrEnum):
    OK = "ok"
    ALREADY = "already"
    DENIED = "denied"


class RemoveKind(StrEnum):
    OK = "ok"
    PROTECTED = "protected"  # владелец или запись из .env
    NOT_FOUND = "not_found"
    DENIED = "denied"


class AdminDirectory:
    def __init__(self, db: Database, clock: Clock, owner_ids: Collection[int]) -> None:
        self._db = db
        self._clock = clock
        self._owner_ids = tuple(owner_ids)

    async def sync(self) -> None:
        """При старте: владельцы из ADMIN_IDS записываются в таблицу, убранные теряют права."""
        async with self._db.transaction() as conn:
            await admins_repo.sync_env_owners(conn, self._owner_ids, self._clock.now())

    async def actor_for(self, tg_id: int) -> Actor | None:
        async with self._db.read() as conn:
            role = await admins_repo.get_role(conn, tg_id)
        return Actor(tg_id, role) if role is not None else None

    async def list(self) -> list[AdminRow]:
        async with self._db.read() as conn:
            return await admins_repo.list_admins(conn)

    async def add_support(self, actor: Actor, tg_id: int) -> AddKind:
        if not actor.can(Permission.ADMINS):
            return AddKind.DENIED
        now = self._clock.now()
        async with self._db.transaction() as conn:
            added = await admins_repo.add_support(conn, tg_id, actor.tg_id, now)
            if added:
                await audit.record(
                    conn, actor, "admin.add", now=now, target_type="admin", target_id=tg_id,
                    role=Role.SUPPORT.value,
                )  # fmt: skip
        return AddKind.OK if added else AddKind.ALREADY

    async def remove_support(self, actor: Actor, tg_id: int) -> RemoveKind:
        if not actor.can(Permission.ADMINS):
            return RemoveKind.DENIED
        now = self._clock.now()
        async with self._db.transaction() as conn:
            role = await admins_repo.get_role(conn, tg_id)
            if role is None:
                return RemoveKind.NOT_FOUND
            removed = await admins_repo.remove_support(conn, tg_id)
            if not removed:
                return RemoveKind.PROTECTED
            await audit.record(
                conn, actor, "admin.remove", now=now, target_type="admin", target_id=tg_id
            )
        return RemoveKind.OK
