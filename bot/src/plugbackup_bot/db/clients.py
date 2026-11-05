"""Клиенты. Здесь единственное место, где меняется поле clients.state."""

import logging
from dataclasses import dataclass

import aiosqlite

from plugbackup_bot.db.users import ensure_user
from plugbackup_bot.domain.logins import make_login, parse_login
from plugbackup_bot.domain.plans import Plan
from plugbackup_bot.domain.states import InvalidTransition, StaleState, State, is_allowed

log = logging.getLogger(__name__)

# Поля, которые можно менять через set_fields. Состояния среди них нет намеренно.
_EDITABLE = frozenset(
    {
        "plan_id",
        "quota_gb",
        "ssh_key_body",
        "expires_at",
        "confirm_due_at",
        "sub_charge_id",
        "sub_payload",
        "renewal_on",
        "admin_hold",
        "admin_note",
        "bonus_gb",
    }
)


@dataclass(frozen=True, slots=True)
class Client:
    id: int
    tg_id: int
    node_id: int
    plan_id: str
    quota_gb: int
    state: State
    ssh_key_body: str | None
    expires_at: int | None
    confirm_due_at: int | None
    state_since: int
    sub_charge_id: str | None
    sub_payload: str | None
    renewal_on: bool
    admin_hold: bool
    created_at: int
    # Заметка администратора и бонусные ГБ сверх тарифа (квота = объём тарифа + bonus_gb).
    admin_note: str | None = None
    bonus_gb: int = 0

    @property
    def login(self) -> str:
        return make_login(self.id)

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "Client":
        return cls(
            id=row["id"],
            tg_id=row["tg_id"],
            node_id=row["node_id"],
            plan_id=row["plan_id"],
            quota_gb=row["quota_gb"],
            state=State(row["state"]),
            ssh_key_body=row["ssh_key_body"],
            expires_at=row["expires_at"],
            confirm_due_at=row["confirm_due_at"],
            state_since=row["state_since"],
            sub_charge_id=row["sub_charge_id"],
            sub_payload=row["sub_payload"],
            renewal_on=bool(row["renewal_on"]),
            admin_hold=bool(row["admin_hold"]),
            created_at=row["created_at"],
            admin_note=row["admin_note"],
            bonus_gb=row["bonus_gb"],
        )


async def create_client(
    conn: aiosqlite.Connection,
    *,
    tg_id: int,
    node_id: int,
    plan: Plan,
    now: int,
    reason: str,
    actor: str,
    expires_at: int | None = None,
    confirm_due_at: int | None = None,
) -> Client:
    """Новый клиент в состоянии pending. Вторую живую запись на аккаунт база не даст."""
    await ensure_user(conn, tg_id, now)
    cur = await conn.execute(
        "INSERT INTO clients (tg_id, node_id, plan_id, quota_gb, state, expires_at,"
        " confirm_due_at, state_since, created_at)"
        " VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
        (tg_id, node_id, plan.id, plan.gb, expires_at, confirm_due_at, now, now),
    )
    client_id = cur.lastrowid
    if client_id is None:
        raise RuntimeError("INSERT не вернул id клиента")
    await conn.execute(
        "INSERT INTO state_log (client_id, from_state, to_state, reason, actor, at)"
        " VALUES (?, NULL, 'pending', ?, ?, ?)",
        (client_id, reason, actor, now),
    )
    log.info("client %s created (%s, %s)", client_id, reason, actor)
    client = await get_client(conn, client_id)
    if client is None:
        raise RuntimeError("созданный клиент не найден")
    return client


async def get_client(conn: aiosqlite.Connection, client_id: int) -> Client | None:
    async with conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)) as cur:
        row = await cur.fetchone()
    return Client.from_row(row) if row else None


async def get_client_by_login(conn: aiosqlite.Connection, login: str) -> Client | None:
    client_id = parse_login(login)
    return await get_client(conn, client_id) if client_id else None


async def get_live_client(conn: aiosqlite.Connection, tg_id: int) -> Client | None:
    """Клиент аккаунта, который ещё не удалён."""
    async with conn.execute(
        "SELECT * FROM clients WHERE tg_id = ? AND state <> 'deleted'", (tg_id,)
    ) as cur:
        row = await cur.fetchone()
    return Client.from_row(row) if row else None


async def list_by_state(conn: aiosqlite.Connection, *states: State) -> list[Client]:
    marks = ", ".join("?" for _ in states)
    async with conn.execute(
        f"SELECT * FROM clients WHERE state IN ({marks}) ORDER BY id",  # noqa: S608
        [state.value for state in states],
    ) as cur:
        rows = await cur.fetchall()
    return [Client.from_row(row) for row in rows]


async def set_fields(conn: aiosqlite.Connection, client_id: int, **fields: object) -> None:
    """Меняет разрешённые поля клиента. Имена полей берутся только из белого списка."""
    unknown = fields.keys() - _EDITABLE
    if unknown:
        raise ValueError(f"поля нельзя менять через set_fields: {', '.join(sorted(unknown))}")
    if not fields:
        return
    assignments = ", ".join(f"{name} = ?" for name in fields)
    cur = await conn.execute(
        f"UPDATE clients SET {assignments} WHERE id = ?",  # noqa: S608
        (*fields.values(), client_id),
    )
    if cur.rowcount != 1:
        raise LookupError(f"клиент {client_id} не найден")


async def transition(
    conn: aiosqlite.Connection,
    client_id: int,
    to: State,
    *,
    reason: str,
    actor: str,
    now: int,
    expected_from: State | None = None,
) -> State:
    """Переводит клиента в новое состояние и пишет журнал. Возвращает прежнее состояние.

    reason — короткий машинный тег («payment», «grace_over»), actor — «system», «client»
    или «admin:<tg_id>». expected_from защищает от гонки: если клиент успел перейти
    в другое состояние, будет StaleState.
    """
    async with conn.execute("SELECT state FROM clients WHERE id = ?", (client_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        raise LookupError(f"клиент {client_id} не найден")
    current = State(row["state"])
    if expected_from is not None and current is not expected_from:
        raise StaleState(client_id, current, to)
    if not is_allowed(current, to, actor):
        raise InvalidTransition(client_id, current, to)
    await conn.execute(
        "UPDATE clients SET state = ?, state_since = ? WHERE id = ?", (to.value, now, client_id)
    )
    await conn.execute(
        "INSERT INTO state_log (client_id, from_state, to_state, reason, actor, at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (client_id, current.value, to.value, reason, actor, now),
    )
    log.info("client %s: %s -> %s (%s, %s)", client_id, current, to, reason, actor)
    return current
