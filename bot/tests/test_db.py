import asyncio
import sqlite3
from pathlib import Path

import pytest

from helpers import NOW, add_client, add_node, add_user
from plugbackup_bot.db import Database, migrate
from plugbackup_bot.db.migrate import load_migrations


async def test_migrate_sets_latest_version(db: Database) -> None:
    assert await db.user_version() == load_migrations()[-1][0]


async def test_migrate_is_idempotent(db: Database) -> None:
    version = await migrate(db)
    assert await migrate(db) == version == await db.user_version()


async def test_newer_schema_is_refused(tmp_path: Path) -> None:
    database = await Database.open(tmp_path / "bot.db")
    try:
        await database.executescript("PRAGMA user_version = 999;")
        with pytest.raises(RuntimeError, match="новее"):
            await migrate(database)
    finally:
        await database.close()


async def test_file_database_uses_wal(db: Database) -> None:
    async with db.read() as conn, conn.execute("PRAGMA journal_mode") as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "wal"


async def test_foreign_keys_are_enforced(db: Database) -> None:
    node_id = await add_node(db)
    with pytest.raises(sqlite3.IntegrityError):
        await add_client(db, tg_id=404, node_id=node_id)


async def test_charge_id_is_unique(db: Database) -> None:
    await add_user(db)
    insert = (
        "INSERT INTO payments (tg_id, plan_id, stars, telegram_payment_charge_id, at)"
        " VALUES (1, 'gb50', 50, 'charge-1', ?)"
    )
    async with db.transaction() as conn:
        await conn.execute(insert, (NOW,))
    with pytest.raises(sqlite3.IntegrityError):
        async with db.transaction() as conn:
            await conn.execute(insert, (NOW,))


async def test_one_live_client_per_user(db: Database) -> None:
    await add_user(db)
    node_id = await add_node(db)
    first = await add_client(db, 1, node_id)
    with pytest.raises(sqlite3.IntegrityError):
        await add_client(db, 1, node_id, state="pending")
    async with db.transaction() as conn:
        await conn.execute("UPDATE clients SET state = 'deleted' WHERE id = ?", (first,))
    second = await add_client(db, 1, node_id, state="pending")
    assert second > first


async def test_client_ids_are_never_reused(db: Database) -> None:
    await add_user(db)
    node_id = await add_node(db)
    first = await add_client(db, 1, node_id, state="deleted")
    async with db.transaction() as conn:
        await conn.execute("DELETE FROM clients WHERE id = ?", (first,))
    assert await add_client(db, 1, node_id) > first


async def test_invalid_state_is_rejected(db: Database) -> None:
    await add_user(db)
    node_id = await add_node(db)
    with pytest.raises(sqlite3.IntegrityError):
        await add_client(db, 1, node_id, state="banned")


async def test_transaction_rolls_back_on_error(db: Database) -> None:
    with pytest.raises(RuntimeError):
        async with db.transaction() as conn:
            await conn.execute("INSERT INTO users (tg_id, first_seen_at) VALUES (7, 1)")
            raise RuntimeError("boom")
    async with db.read() as conn, conn.execute("SELECT COUNT(*) FROM users") as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == 0


async def test_transactions_are_serialized(db: Database) -> None:
    """Читатель не видит незавершённую транзакцию соседней корутины."""
    seen: list[int] = []

    async def writer() -> None:
        with pytest.raises(RuntimeError):
            async with db.transaction() as conn:
                await conn.execute("INSERT INTO users (tg_id, first_seen_at) VALUES (9, 1)")
                await asyncio.sleep(0.05)
                raise RuntimeError("rollback")

    async def reader() -> None:
        await asyncio.sleep(0.01)
        async with db.read() as conn, conn.execute("SELECT COUNT(*) FROM users") as cur:
            row = await cur.fetchone()
            assert row is not None
            seen.append(row[0])

    await asyncio.gather(writer(), reader())
    assert seen == [0]
