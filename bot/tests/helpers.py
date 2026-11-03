"""Заготовки строк в базе для тестов. Пишут напрямую SQL, в обход кода бота."""

import base64

from plugbackup_bot.db import Database
from plugbackup_bot.domain.plans import Plan, PlanCatalog

NOW = 1_760_000_000

CATALOG = PlanCatalog(
    [
        Plan(id="beta", name="Бета", gb=10, price_rub=0, stars=0),
        Plan(id="gb50", name="50 ГБ", gb=50, price_rub=79, stars=50),
        Plan(id="gb100", name="100 ГБ", gb=100, price_rub=149, stars=100),
        Plan(id="gb200", name="200 ГБ", gb=200, price_rub=249, stars=170),
    ]
)


async def add_user(db: Database, tg_id: int = 1) -> None:
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO users (tg_id, first_seen_at) VALUES (?, ?)", (tg_id, NOW)
        )


async def add_node(
    db: Database, name: str = "node1", capacity_gb: int = 100, oversell: float = 1.0
) -> int:
    async with db.transaction() as conn:
        cur = await conn.execute(
            "INSERT INTO nodes (name, addr, public_host, capacity_gb, oversell)"
            " VALUES (?, '192.0.2.10', 'node1.example.org', ?, ?)",
            (name, capacity_gb, oversell),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid


async def add_client(
    db: Database,
    tg_id: int,
    node_id: int,
    state: str = "active",
    plan_id: str = "gb50",
    quota_gb: int = 50,
    ssh_key_body: str | None = None,
) -> int:
    async with db.transaction() as conn:
        cur = await conn.execute(
            "INSERT INTO clients"
            " (tg_id, node_id, plan_id, quota_gb, state, ssh_key_body, state_since, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (tg_id, node_id, plan_id, quota_gb, state, ssh_key_body, NOW, NOW),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid


_KEY_PREFIX = (11).to_bytes(4, "big") + b"ssh-ed25519" + (32).to_bytes(4, "big")


def fake_key_body(seed: int = 0) -> str:
    """Тело публичного ключа ed25519 для тестов. Строится на лету, чтобы в исходниках не
    лежали длинные base64-строки, которые сканеры секретов принимают за ключи."""
    return base64.b64encode(_KEY_PREFIX + bytes(range(seed, seed + 32))).decode("ascii")
