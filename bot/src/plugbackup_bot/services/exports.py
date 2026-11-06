"""Выгрузки для владельца: клиенты и платежи в CSV и копия базы.

В выгрузках нет SSH-ключей, заметок и идентификаторов подписок. Ячейки, которые таблица могла бы
принять за формулу, экранируются (защита от CSV-инъекций). Копия базы делается средствами
SQLite (VACUUM INTO) на живой базе и содержит всё: передавать её можно только владельцу.
"""

import csv
import io
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from plugbackup_bot.db import Database, audit
from plugbackup_bot.domain.logins import make_login
from plugbackup_bot.domain.roles import Actor, Permission
from plugbackup_bot.infra.clock import Clock

DELIMITER = ";"
# Таблица принимает такую ячейку за формулу, если она начинается с одного из этих знаков.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
# Предел Telegram на файл от бота.
MAX_FILE_BYTES = 50 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ExportFile:
    name: str
    data: bytes
    rows: int


def safe_cell(value: object) -> object:
    """Текст, похожий на формулу, получает впереди апостроф: таблица покажет его как текст."""
    if isinstance(value, str) and value.startswith(_FORMULA_START):
        return "'" + value
    return value


def to_csv(header: list[str], rows: list[list[object]]) -> bytes:
    """CSV в UTF-8 с BOM, чтобы Excel не ломал кириллицу."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=DELIMITER, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([safe_cell(cell) for cell in row])
    return buffer.getvalue().encode("utf-8-sig")


class ExportService:
    def __init__(self, db: Database, clock: Clock, tz: str) -> None:
        self._db = db
        self._clock = clock
        self._zone = ZoneInfo(tz)

    def _stamp(self) -> str:
        return datetime.fromtimestamp(self._clock.now(), self._zone).strftime("%Y%m%d-%H%M")

    def _iso(self, ts: int | None) -> str:
        if ts is None:
            return ""
        return datetime.fromtimestamp(ts, self._zone).strftime("%Y-%m-%d %H:%M")

    async def clients(self, actor: Actor) -> ExportFile | None:
        if not actor.can(Permission.EXPORT):
            return None
        async with (
            self._db.read() as conn,
            conn.execute(
                "SELECT c.id, c.tg_id, u.username, c.plan_id, c.state, c.quota_gb, c.bonus_gb,"
                " n.name AS node, c.created_at, c.expires_at, c.renewal_on, c.admin_hold"
                " FROM clients c LEFT JOIN users u ON u.tg_id = c.tg_id"
                " LEFT JOIN nodes n ON n.id = c.node_id ORDER BY c.id"
            ) as cur,
        ):
            found = await cur.fetchall()
        rows: list[list[object]] = [
            [
                make_login(r["id"]), r["tg_id"], r["username"] or "", r["plan_id"], r["state"],
                r["quota_gb"], r["bonus_gb"], r["node"] or "", self._iso(r["created_at"]),
                self._iso(r["expires_at"]), r["renewal_on"], r["admin_hold"],
            ]
            for r in found
        ]  # fmt: skip
        header = [
            "login", "tg_id", "username", "plan", "state", "quota_gb", "bonus_gb", "node",
            "created", "expires", "renewal", "admin_hold",
        ]  # fmt: skip
        return await self._finish(actor, "clients", f"clients-{self._stamp()}.csv", header, rows)

    async def payments(self, actor: Actor) -> ExportFile | None:
        if not actor.can(Permission.EXPORT):
            return None
        async with (
            self._db.read() as conn,
            conn.execute(
                "SELECT p.id, p.at, p.tg_id, p.client_id, u.username, p.plan_id, p.stars,"
                " p.is_first, p.is_recurring, p.status, p.refunded_at,"
                " p.telegram_payment_charge_id AS charge"
                " FROM payments p LEFT JOIN users u ON u.tg_id = p.tg_id ORDER BY p.id"
            ) as cur,
        ):
            found = await cur.fetchall()
        rows: list[list[object]] = [
            [
                r["id"], self._iso(r["at"]), r["tg_id"],
                make_login(r["client_id"]) if r["client_id"] is not None else "",
                r["username"] or "", r["plan_id"], r["stars"],
                "first" if r["is_first"] else ("renewal" if r["is_recurring"] else "single"),
                r["status"], self._iso(r["refunded_at"]), r["charge"],
            ]
            for r in found
        ]  # fmt: skip
        header = [
            "id", "date", "tg_id", "login", "username", "plan", "stars", "kind", "status",
            "refunded", "charge_id",
        ]  # fmt: skip
        return await self._finish(actor, "payments", f"payments-{self._stamp()}.csv", header, rows)

    async def _finish(
        self, actor: Actor, what: str, name: str, header: list[str], rows: list[list[object]]
    ) -> ExportFile:
        data = to_csv(header, rows)
        async with self._db.transaction() as conn:
            await audit.record(
                conn, actor, f"export.{what}", now=self._clock.now(), rows=len(rows),
            )  # fmt: skip
        return ExportFile(name, data, len(rows))

    async def backup(self, actor: Actor) -> ExportFile | None:
        """Целостная копия базы на момент вызова. None, если у роли нет права."""
        if not actor.can(Permission.BACKUP):
            return None
        name = f"plugbackup-{self._stamp()}.db"
        with tempfile.TemporaryDirectory(prefix="plugbackup-backup-") as folder:
            target = Path(folder) / name
            async with self._db.read() as conn:
                await conn.execute("VACUUM INTO ?", (str(target),))
            data = target.read_bytes()
        async with self._db.transaction() as conn:
            await audit.record(conn, actor, "export.backup", now=self._clock.now(), size=len(data))
        return ExportFile(name, data, 0)
