"""Рассылки администратора: сегменты, кнопки, отложенная отправка, статистика.

Получатели записываются в broadcast_recipients в момент начала отправки и отмечаются по одному,
поэтому рассылка, прерванная перезапуском, продолжается с того же места и никому не уходит
дважды. Скорость ограничена (по умолчанию 20 сообщений в секунду, лимит Telegram около 30), при
flood wait ожидание делает отправитель (services.notify).
"""

import asyncio
import html
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from plugbackup_bot.db import Database, audit
from plugbackup_bot.domain.plans import PlanCatalog
from plugbackup_bot.domain.roles import Actor
from plugbackup_bot.infra.clock import Clock
from plugbackup_bot.services.notify import Delivery, Messenger

log = logging.getLogger(__name__)

MAX_TEXT = 3500
MAX_BUTTONS = 3
MAX_BUTTON_LABEL = 40
MAX_SCHEDULE_DAYS = 30
EXPIRING_DAYS = 7
_DAY = 24 * 3600
_BATCH = 25
_URL_SCHEMES = ("https", "http", "tg")


class Segment(StrEnum):
    ALL = "all"
    BETA = "beta"
    PAID = "paid"
    EXPIRING = "expiring"
    BLOCKED = "blocked"


NODE_SEGMENT_PREFIX = "node:"


def _is_node_segment(segment: str) -> bool:
    return (
        segment.startswith(NODE_SEGMENT_PREFIX) and segment[len(NODE_SEGMENT_PREFIX) :].isdecimal()
    )


class BroadcastStatus(StrEnum):
    SCHEDULED = "scheduled"
    SENDING = "sending"
    DONE = "done"
    CANCELLED = "cancelled"


class ButtonProblem(ValueError):
    """Строка кнопки не разобрана: сообщение понятно администратору."""


@dataclass(frozen=True, slots=True)
class Broadcast:
    id: int
    text: str
    segment: str
    buttons: tuple[tuple[str, str], ...]
    status: BroadcastStatus
    send_at: int | None
    created_by: int
    created_at: int
    started_at: int | None
    finished_at: int | None
    kind: str
    total: int
    sent: int
    blocked: int
    failed: int

    @property
    def pending(self) -> int:
        return max(0, self.total - self.sent - self.blocked - self.failed)


def parse_buttons(raw: str) -> tuple[tuple[str, str], ...]:
    """Кнопки из текста: по строке на кнопку, «Подпись | https://адрес». Пусто: без кнопок."""
    found: list[tuple[str, str]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        label, sep, url = line.rpartition("|")
        label, url = label.strip(), url.strip()
        if not sep or not label or not url:
            raise ButtonProblem("нужна строка вида «Подпись | https://адрес»")
        if len(label) > MAX_BUTTON_LABEL:
            raise ButtonProblem(f"подпись не длиннее {MAX_BUTTON_LABEL} знаков")
        parts = urlsplit(url)
        if parts.scheme not in _URL_SCHEMES or not (parts.netloc or parts.scheme == "tg"):
            raise ButtonProblem("адрес должен начинаться с https://, http:// или tg://")
        if any(ch.isspace() for ch in url) or len(url) > 512:
            raise ButtonProblem("адрес без пробелов и не длиннее 512 знаков")
        found.append((label, url))
    if len(found) > MAX_BUTTONS:
        raise ButtonProblem(f"кнопок не больше {MAX_BUTTONS}")
    return tuple(found)


def parse_send_time(raw: str, tz: str, now: int) -> int:
    """Время отправки из «ДД.ММ ЧЧ:ММ» или «ДД.ММ.ГГГГ ЧЧ:ММ» в часовом поясе tz.

    Без года берётся ближайшая такая дата в будущем. ValueError: не разобрано или не в
    допустимом окне (в будущем и не позже MAX_SCHEDULE_DAYS дней).
    """
    zone = ZoneInfo(tz)
    text = " ".join(raw.split())
    today = datetime.fromtimestamp(now, zone)
    moment: datetime | None = None
    with_year = False
    for pattern in ("%d.%m.%Y %H:%M", "%d.%m %H:%M"):
        try:
            moment = datetime.strptime(text, pattern)
        except ValueError:
            continue
        with_year = "%Y" in pattern
        break
    if moment is None:
        raise ValueError("время не разобрано")
    if not with_year:
        try:
            moment = moment.replace(year=today.year)
        except ValueError as error:  # 29 февраля в невисокосный год
            raise ValueError("время не разобрано") from error
    moment = moment.replace(tzinfo=zone)
    if not with_year and moment.timestamp() <= now:
        try:
            moment = moment.replace(year=today.year + 1)
        except ValueError as error:
            raise ValueError("время не разобрано") from error
    stamp = int(moment.timestamp())
    if not now < stamp <= now + MAX_SCHEDULE_DAYS * _DAY:
        raise ValueError("время вне допустимого окна")
    return stamp


def to_markup(buttons: Sequence[tuple[str, str]]) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    rows = [[InlineKeyboardButton(text=label, url=url)] for label, url in buttons]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _count(status: str, alias: str) -> str:
    return (
        "(SELECT COUNT(*) FROM broadcast_recipients r"  # noqa: S608
        f" WHERE r.broadcast_id = b.id AND r.status = '{status}') AS {alias}"
    )


_COLUMNS = ", ".join(
    [
        "b.id", "b.text", "b.segment", "b.buttons_json", "b.status", "b.send_at",
        "b.created_by", "b.created_at", "b.started_at", "b.finished_at", "b.kind", "b.total",
        _count("sent", "n_sent"), _count("blocked", "n_blocked"), _count("failed", "n_failed"),
        "b.sent AS legacy_sent", "b.failed AS legacy_failed",
    ]
)  # fmt: skip


def _broadcast(row: aiosqlite.Row) -> Broadcast:
    try:
        buttons = tuple((str(label), str(url)) for label, url in json.loads(row["buttons_json"]))
    except (ValueError, TypeError):
        buttons = ()
    status = BroadcastStatus(row["status"])
    n_sent, n_blocked, n_failed = row["n_sent"], row["n_blocked"], row["n_failed"]
    if status is BroadcastStatus.DONE and not row["total"]:
        # Старые рассылки (до списка получателей) хранили счётчики прямо в строке.
        n_sent, n_failed = row["legacy_sent"], row["legacy_failed"]
    return Broadcast(
        id=row["id"], text=row["text"], segment=row["segment"], buttons=buttons, status=status,
        send_at=row["send_at"], created_by=row["created_by"], created_at=row["created_at"],
        started_at=row["started_at"], finished_at=row["finished_at"], kind=row["kind"],
        total=row["total"], sent=n_sent, blocked=n_blocked, failed=n_failed,
    )  # fmt: skip


class BroadcastService:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        messenger: Messenger,
        catalog: PlanCatalog,
        *,
        rate_per_s: float = 20,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._db = db
        self._clock = clock
        self._messenger = messenger
        self._catalog = catalog
        self._delay = 1 / rate_per_s
        self._sleep = sleep
        self._lock = asyncio.Lock()

    # --- получатели ---

    async def _segment_ids(self, conn: aiosqlite.Connection, segment: str) -> list[int]:
        now = self._clock.now()
        beta = self._catalog.beta.id
        live = "c.state IN ('active', 'expiring')"
        select = "SELECT c.tg_id FROM clients c JOIN users u ON u.tg_id = c.tg_id"
        reachable = "u.bot_blocked = 0"
        params: tuple[object, ...] = ()
        if segment == Segment.ALL:
            sql = "SELECT tg_id FROM users WHERE bot_blocked = 0"
        elif segment == Segment.BETA:
            sql, params = f"{select} WHERE {live} AND {reachable} AND c.plan_id = ?", (beta,)
        elif segment == Segment.PAID:
            sql, params = f"{select} WHERE {live} AND {reachable} AND c.plan_id <> ?", (beta,)
        elif segment == Segment.EXPIRING:
            due = "CASE WHEN c.plan_id = ? THEN c.confirm_due_at ELSE c.expires_at END"
            sql = f"{select} WHERE {live} AND {reachable} AND {due} BETWEEN ? AND ?"
            params = (beta, now, now + EXPIRING_DAYS * _DAY)
        elif segment == Segment.BLOCKED:
            sql = f"{select} WHERE c.state = 'suspended' AND {reachable}"
        elif _is_node_segment(segment):
            states = "c.state IN ('active', 'expiring', 'suspended')"
            sql = f"{select} WHERE {states} AND {reachable} AND c.node_id = ?"
            params = (int(segment.removeprefix(NODE_SEGMENT_PREFIX)),)
        else:
            raise ValueError(f"неизвестный сегмент: {segment!r}")
        async with conn.execute(sql, params) as cur:
            return sorted({int(row[0]) for row in await cur.fetchall()})

    async def recipients(self, segment: str = Segment.ALL) -> list[int]:
        async with self._db.read() as conn:
            return await self._segment_ids(conn, segment)

    async def segment_counts(self) -> dict[str, int]:
        async with self._db.read() as conn:
            return {seg.value: len(await self._segment_ids(conn, seg)) for seg in Segment}

    # --- создание и отмена ---

    async def create(
        self,
        actor: Actor,
        *,
        segment: str,
        text: str,
        buttons: Sequence[tuple[str, str]] = (),
        send_at: int | None = None,
        kind: str = "manual",
    ) -> int:
        """Ставит рассылку в очередь. send_at None: отправить как можно скорее."""
        text = text.strip()
        if not text or len(text) > MAX_TEXT:
            raise ValueError(f"текст рассылки: от 1 до {MAX_TEXT} символов")
        if len(buttons) > MAX_BUTTONS:
            raise ValueError("слишком много кнопок")
        now = self._clock.now()
        if send_at is not None and not now < send_at <= now + MAX_SCHEDULE_DAYS * _DAY:
            raise ValueError(
                f"время отправки: в будущем и не позже чем через {MAX_SCHEDULE_DAYS} дней"
            )
        async with self._db.transaction() as conn:
            await self._segment_ids(conn, segment)  # проверка сегмента
            cur = await conn.execute(
                "INSERT INTO broadcasts (text, created_by, created_at, segment, buttons_json,"
                " status, send_at, kind) VALUES (?, ?, ?, ?, ?, 'scheduled', ?, ?)",
                (text, actor.tg_id, now, segment, json.dumps(list(map(list, buttons))),
                 send_at or now, kind),
            )  # fmt: skip
            broadcast_id = int(cur.lastrowid or 0)
            await audit.record(
                conn, actor, "broadcast.create", now=now, target_type="broadcast",
                target_id=broadcast_id, segment=segment, kind=kind, scheduled=send_at is not None,
                length=len(text),
            )  # fmt: skip
        return broadcast_id

    async def cancel(self, broadcast_id: int, actor: Actor) -> bool:
        """Отменяет рассылку, которая ещё не закончилась. Уже отправленное не вернуть."""
        now = self._clock.now()
        async with self._db.transaction() as conn:
            cur = await conn.execute(
                "UPDATE broadcasts SET status = 'cancelled', finished_at = ?"
                " WHERE id = ? AND status IN ('scheduled', 'sending')",
                (now, broadcast_id),
            )
            if cur.rowcount != 1:
                return False
            await audit.record(
                conn, actor, "broadcast.cancel", now=now, target_type="broadcast",
                target_id=broadcast_id,
            )  # fmt: skip
        return True

    # --- чтение ---

    async def get(self, broadcast_id: int) -> Broadcast | None:
        async with (
            self._db.read() as conn,
            conn.execute(
                f"SELECT {_COLUMNS} FROM broadcasts b WHERE b.id = ?",  # noqa: S608
                (broadcast_id,),
            ) as cur,
        ):
            row = await cur.fetchone()
        return _broadcast(row) if row else None

    async def page(self, *, limit: int, offset: int) -> tuple[list[Broadcast], int]:
        async with self._db.read() as conn:
            async with conn.execute("SELECT COUNT(*) FROM broadcasts") as cur:
                total = int((await cur.fetchone())[0])  # type: ignore[index]
            async with conn.execute(
                f"SELECT {_COLUMNS} FROM broadcasts b"  # noqa: S608
                " ORDER BY b.id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ) as cur:
                rows = [_broadcast(row) for row in await cur.fetchall()]
        return rows, total

    async def open_count(self) -> int:
        async with (
            self._db.read() as conn,
            conn.execute(
                "SELECT COUNT(*) FROM broadcasts WHERE status IN ('scheduled', 'sending')"
            ) as cur,
        ):
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    # --- отправка ---

    async def send_test(self, tg_id: int, text: str, buttons: Sequence[tuple[str, str]]) -> bool:
        """Пробное сообщение только администратору: так его увидят получатели."""
        outcome = await self._messenger.deliver(
            tg_id, html.escape(text), reply_markup=to_markup(buttons)
        )
        return outcome is Delivery.SENT

    async def run_due(self) -> int:
        """Отправляет всё, что пора отправлять, и продолжает незаконченное. Возвращает число
        отправленных сообщений. Параллельные запуски ждут друг друга."""
        async with self._lock:
            now = self._clock.now()
            async with (
                self._db.read() as conn,
                conn.execute(
                    "SELECT id FROM broadcasts WHERE status IN ('scheduled', 'sending')"
                    " AND COALESCE(send_at, 0) <= ? ORDER BY id",
                    (now,),
                ) as cur,
            ):
                due = [int(row[0]) for row in await cur.fetchall()]
            sent = 0
            for broadcast_id in due:
                try:
                    sent += await self._run_one(broadcast_id)
                except Exception:
                    log.exception("broadcast %s failed", broadcast_id)
            return sent

    async def _run_one(self, broadcast_id: int) -> int:
        broadcast = await self._start(broadcast_id)
        if broadcast is None:
            return 0
        body = html.escape(broadcast.text)
        markup = to_markup(broadcast.buttons)
        sent = 0
        while True:
            async with self._db.read() as conn:
                async with conn.execute(
                    "SELECT status FROM broadcasts WHERE id = ?", (broadcast_id,)
                ) as cur:
                    row = await cur.fetchone()
                if row is None or row["status"] != BroadcastStatus.SENDING:
                    return sent  # отменена
                async with conn.execute(
                    "SELECT tg_id FROM broadcast_recipients WHERE broadcast_id = ?"
                    " AND status = 'pending' ORDER BY tg_id LIMIT ?",
                    (broadcast_id, _BATCH),
                ) as cur:
                    batch = [int(r[0]) for r in await cur.fetchall()]
            if not batch:
                break
            for tg_id in batch:
                try:
                    outcome = await self._messenger.deliver(tg_id, body, reply_markup=markup)
                except Exception:
                    log.exception("broadcast %s to %s failed", broadcast_id, tg_id)
                    outcome = Delivery.FAILED
                await self._mark(broadcast_id, tg_id, outcome)
                sent += outcome is Delivery.SENT
                await self._sleep(self._delay)
        await self._finish(broadcast_id)
        return sent

    async def _start(self, broadcast_id: int) -> Broadcast | None:
        """Переводит рассылку в «идёт отправка» и один раз записывает получателей."""
        now = self._clock.now()
        async with self._db.transaction() as conn:
            async with conn.execute(
                f"SELECT {_COLUMNS} FROM broadcasts b WHERE b.id = ?",  # noqa: S608
                (broadcast_id,),
            ) as cur:
                row = await cur.fetchone()
            if row is None or row["status"] not in ("scheduled", "sending"):
                return None
            broadcast = _broadcast(row)
            if broadcast.status is BroadcastStatus.SCHEDULED:
                ids = await self._segment_ids(conn, broadcast.segment)
                await conn.executemany(
                    "INSERT OR IGNORE INTO broadcast_recipients (broadcast_id, tg_id)"
                    " VALUES (?, ?)",
                    [(broadcast_id, tg_id) for tg_id in ids],
                )
                await conn.execute(
                    "UPDATE broadcasts SET status = 'sending', started_at = ?, total = ?"
                    " WHERE id = ?",
                    (now, len(ids), broadcast_id),
                )
        return broadcast

    async def _mark(self, broadcast_id: int, tg_id: int, outcome: Delivery) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                "UPDATE broadcast_recipients SET status = ?, sent_at = ?"
                " WHERE broadcast_id = ? AND tg_id = ?",
                (outcome.value, self._clock.now(), broadcast_id, tg_id),
            )

    async def _finish(self, broadcast_id: int) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                "UPDATE broadcasts SET status = 'done', finished_at = ?,"
                " sent = (SELECT COUNT(*) FROM broadcast_recipients"
                "         WHERE broadcast_id = ? AND status = 'sent'),"
                " failed = (SELECT COUNT(*) FROM broadcast_recipients"
                "           WHERE broadcast_id = ? AND status IN ('failed', 'blocked')),"
                " blocked = (SELECT COUNT(*) FROM broadcast_recipients"
                "            WHERE broadcast_id = ? AND status = 'blocked')"
                " WHERE id = ? AND status = 'sending'",
                (self._clock.now(), broadcast_id, broadcast_id, broadcast_id, broadcast_id),
            )
