"""Тарифы, которые админ правит из бота: цена в Stars, объём, ориентир в рублях, видимость.

Изменения действуют на новые покупки. Уже оплаченные подписки Stars и квоты клиентов не
меняются: цену подписки Telegram хранит у себя, а квота лежит в clients.quota_gb.
"""

import logging
from collections.abc import Sequence
from enum import StrEnum

from plugbackup_bot.config import MAX_PRICE_STARS
from plugbackup_bot.db import Database, audit
from plugbackup_bot.db import plans as plans_repo
from plugbackup_bot.domain.plans import Plan, PlanCatalog
from plugbackup_bot.domain.roles import Actor
from plugbackup_bot.infra.clock import Clock

log = logging.getLogger(__name__)

MAX_GB = 100_000
MAX_PRICE_RUB = 1_000_000
_NBSP = " "


class PlanResult(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"
    INVALID = "invalid"
    DUPLICATE_GB = "duplicate_gb"
    BETA = "beta"  # у беты нельзя менять цену и видимость


def _auto_name(plan: Plan) -> bool:
    """Название вида «50 ГБ», которое выводится из объёма и должно следовать за ним."""
    return plan.name.replace(_NBSP, " ") == f"{plan.gb} ГБ"


class PlanService:
    def __init__(self, db: Database, clock: Clock, catalog: PlanCatalog) -> None:
        self._db = db
        self._clock = clock
        self._catalog = catalog

    async def seed(self, plans: Sequence[Plan]) -> int:
        """При запуске: добавляет тарифы из файлов, которых ещё нет в базе."""
        async with self._db.transaction() as conn:
            return await plans_repo.insert_missing(conn, plans, self._clock.now())

    async def load(self) -> None:
        """Перечитывает тарифы из базы в общий каталог."""
        async with self._db.read() as conn:
            plans = await plans_repo.list_plans(conn)
        self._catalog.replace(plans)

    async def client_counts(self) -> dict[str, int]:
        """Сколько живых клиентов на каждом тарифе."""
        async with (
            self._db.read() as conn,
            conn.execute(
                "SELECT plan_id, COUNT(*) FROM clients WHERE state <> 'deleted' GROUP BY plan_id"
            ) as cur,
        ):
            return {row[0]: int(row[1]) for row in await cur.fetchall()}

    # --- правки ---

    async def set_stars(self, plan_id: str, actor: Actor, stars: int) -> PlanResult:
        if not 1 <= stars <= MAX_PRICE_STARS:
            return PlanResult.INVALID
        return await self._update(plan_id, actor, "tariff.stars", "stars", stars, paid_only=True)

    async def set_price_rub(self, plan_id: str, actor: Actor, price_rub: int) -> PlanResult:
        if not 1 <= price_rub <= MAX_PRICE_RUB:
            return PlanResult.INVALID
        return await self._update(
            plan_id, actor, "tariff.rub", "price_rub", price_rub, paid_only=True
        )

    async def set_gb(self, plan_id: str, actor: Actor, gb: int) -> PlanResult:
        if not 1 <= gb <= MAX_GB:
            return PlanResult.INVALID
        return await self._update(plan_id, actor, "tariff.gb", "gb", gb)

    async def set_visible(self, plan_id: str, actor: Actor, visible: bool) -> PlanResult:
        action = "tariff.show" if visible else "tariff.hide"
        return await self._update(plan_id, actor, action, "visible", visible, paid_only=True)

    async def _update(
        self,
        plan_id: str,
        actor: Actor,
        action: str,
        field: str,
        value: int | bool,
        *,
        paid_only: bool = False,
    ) -> PlanResult:
        now = self._clock.now()
        async with self._db.transaction() as conn:
            plan = await plans_repo.get_plan(conn, plan_id)
            if plan is None:
                return PlanResult.NOT_FOUND
            if paid_only and plan.is_beta:
                return PlanResult.BETA
            changes: dict[str, object] = {field: value}
            if field == "gb":
                others = [p for p in await plans_repo.list_plans(conn) if p.id != plan_id]
                if any(p.gb == value and not p.is_beta and not plan.is_beta for p in others):
                    return PlanResult.DUPLICATE_GB
                if _auto_name(plan):
                    changes["name"] = f"{value} ГБ"
            before = getattr(plan, field)
            await plans_repo.update_fields(conn, plan_id, now, **changes)
            await audit.record(
                conn, actor, action, now=now, target_type="tariff", target_id=plan_id,
                before=int(before), after=int(value),
            )  # fmt: skip
        await self.load()
        return PlanResult.OK
