"""Платежи в админке: история, поиск и возврат Stars.

Возврат идёт через Bot API (refundStarPayment). Сначала просим Telegram, и только если он вернул
деньги (или сообщил, что они уже возвращены), помечаем платёж в базе и, если выбрано, отключаем
доступ клиента и автопродление. Так база не расходится с Telegram при сбое.
"""

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import aiosqlite

from plugbackup_bot.db import Database, audit
from plugbackup_bot.db.clients import get_client
from plugbackup_bot.db.users import find_by_username
from plugbackup_bot.domain.logins import make_login
from plugbackup_bot.domain.roles import Actor, Permission
from plugbackup_bot.domain.states import State
from plugbackup_bot.infra.clock import Clock
from plugbackup_bot.services.admin import AdminService
from plugbackup_bot.services.billing import BillingService

log = logging.getLogger(__name__)

MIN_CHARGE_QUERY = 4
MAX_REASON = 200
_ALREADY_REFUNDED = "already_refunded"


class StarsRefunder(Protocol):
    async def refund(self, tg_id: int, charge_id: str) -> None:
        """Возвращает Stars за платёж. При отказе Telegram бросает исключение."""
        ...


class RefundKind(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"
    ALREADY = "already"  # в нашей базе платёж уже возвращён
    FAILED = "failed"  # Telegram отказал: платёж не тронут
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class PaymentView:
    id: int
    tg_id: int
    client_id: int | None
    login: str | None
    username: str | None
    plan_id: str
    stars: int
    charge_id: str
    is_recurring: bool
    is_first: bool
    status: str
    at: int
    refunded_at: int | None
    refunded_by: int | None
    refund_revoked: bool
    # Последний ли это оплаченный платёж клиента: отключать доступ имеет смысл только за него.
    is_latest: bool


@dataclass(frozen=True, slots=True)
class RefundResult:
    kind: RefundKind
    payment: PaymentView | None = None
    reason: str | None = None
    access_revoked: bool = False


_SELECT = (
    "SELECT p.*, u.username AS username,"
    " (SELECT NOT EXISTS (SELECT 1 FROM payments q WHERE q.client_id = p.client_id"
    "    AND q.status = 'paid' AND (q.at > p.at OR (q.at = p.at AND q.id > p.id)))) AS is_latest"
    " FROM payments p LEFT JOIN users u ON u.tg_id = p.tg_id"
)


def _view(row: aiosqlite.Row) -> PaymentView:
    client_id = row["client_id"]
    return PaymentView(
        id=row["id"], tg_id=row["tg_id"], client_id=client_id,
        login=make_login(client_id) if client_id is not None else None,
        username=row["username"], plan_id=row["plan_id"], stars=row["stars"],
        charge_id=row["telegram_payment_charge_id"], is_recurring=bool(row["is_recurring"]),
        is_first=bool(row["is_first"]), status=row["status"], at=row["at"],
        refunded_at=row["refunded_at"], refunded_by=row["refunded_by"],
        refund_revoked=bool(row["refund_revoked"]), is_latest=bool(row["is_latest"]),
    )  # fmt: skip


def _like(text: str) -> str:
    """Подстрока для LIKE: служебные знаки экранируются."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class PaymentsAdmin:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        stars: StarsRefunder,
        billing: BillingService,
        admin: AdminService,
    ) -> None:
        self._db = db
        self._clock = clock
        self._stars = stars
        self._billing = billing
        self._admin = admin

    # --- чтение ---

    async def _filter(self, query: str | None) -> tuple[str, tuple[object, ...]] | None:
        """Условие поиска: логин, Telegram id, @username или часть номера платежа Telegram.
        None: ничего не найдено заранее."""
        query = (query or "").strip()
        if not query:
            return "1 = 1", ()
        if query.isdecimal() and len(query) <= 15:
            return "p.tg_id = ?", (int(query),)
        if query.startswith("@"):
            async with self._db.read() as conn:
                tg_id = await find_by_username(conn, query)
            return ("p.tg_id = ?", (tg_id,)) if tg_id is not None else None
        client = await self._admin.find_client(query)
        if client is not None:
            return "p.tg_id = ?", (client.tg_id,)
        if len(query) >= MIN_CHARGE_QUERY:
            return "p.telegram_payment_charge_id LIKE ? ESCAPE '\\'", (_like(query),)
        return None

    async def page(
        self, query: str | None, *, limit: int, offset: int
    ) -> tuple[list[PaymentView], int]:
        found = await self._filter(query)
        if found is None:
            return [], 0
        where, params = found
        async with self._db.read() as conn:
            async with conn.execute(
                f"SELECT COUNT(*) FROM payments p WHERE {where}",  # noqa: S608
                params,
            ) as cur:
                total = int((await cur.fetchone())[0])  # type: ignore[index]
            async with conn.execute(
                f"{_SELECT} WHERE {where} ORDER BY p.at DESC, p.id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ) as cur:
                rows = [_view(row) for row in await cur.fetchall()]
        return rows, total

    async def get(self, payment_id: int) -> PaymentView | None:
        async with (
            self._db.read() as conn,
            conn.execute(f"{_SELECT} WHERE p.id = ?", (payment_id,)) as cur,
        ):
            row = await cur.fetchone()
        return _view(row) if row else None

    # --- возврат ---

    async def refund(self, payment_id: int, actor: Actor, *, revoke_access: bool) -> RefundResult:
        if not actor.can(Permission.REFUNDS):
            return RefundResult(RefundKind.DENIED)
        payment = await self.get(payment_id)
        if payment is None:
            return RefundResult(RefundKind.NOT_FOUND)
        if payment.status == "refunded":
            return RefundResult(RefundKind.ALREADY, payment)
        try:
            await self._stars.refund(payment.tg_id, payment.charge_id)
        except Exception as error:
            if _ALREADY_REFUNDED not in str(error).lower():
                log.warning("refund of payment %s failed", payment_id, exc_info=True)
                return RefundResult(RefundKind.FAILED, payment, reason=str(error)[:MAX_REASON])
            log.info("payment %s was already refunded in Telegram", payment_id)
        revoke = revoke_access and payment.client_id is not None and payment.is_latest
        await self._mark(payment, actor, revoke)
        revoked = False
        if revoke and payment.client_id is not None:
            revoked = await self._revoke(payment.client_id, actor)
        return RefundResult(RefundKind.OK, await self.get(payment_id), access_revoked=revoked)

    async def _mark(self, payment: PaymentView, actor: Actor, revoke: bool) -> None:
        now = self._clock.now()
        async with self._db.transaction() as conn:
            await conn.execute(
                "UPDATE payments SET status = 'refunded', refunded_at = ?, refunded_by = ?,"
                " refund_revoked = ? WHERE id = ? AND status = 'paid'",
                (now, actor.tg_id, int(revoke), payment.id),
            )
            await audit.record(
                conn, actor, "payment.refund", now=now, target_type="payment",
                target_id=payment.id, stars=payment.stars, revoke_access=revoke,
            )  # fmt: skip

    async def _revoke(self, client_id: int, actor: Actor) -> bool:
        """Отключает автопродление и приостанавливает доступ. True, если клиент не работает."""
        async with self._db.read() as conn:
            client = await get_client(conn, client_id)
        if client is None:
            return False
        if client.renewal_on:
            await self._billing.set_renewal(client, False)
        await self._admin.suspend(client_id, actor)
        async with self._db.read() as conn:
            fresh = await get_client(conn, client_id)
        return fresh is not None and fresh.state in (State.SUSPENDED, State.DELETED)
