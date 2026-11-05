# ruff: noqa: E501
"""Миграции схемы: обновление базы с данными, а не только пустой."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio

from plugbackup_bot.db import Database, migrate
from plugbackup_bot.db.migrate import load_migrations

NOW = 1_760_000_000


@pytest_asyncio.fixture
async def old_db(tmp_path: Path) -> AsyncIterator[Database]:
    """База версии 1 с данными: так выглядит боевая база до обновления."""
    database = await Database.open(tmp_path / "old.db")
    number, _, sql = load_migrations()[0]
    await database.executescript(
        f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;"
    )
    async with database.transaction() as conn:
        await conn.execute(
            "INSERT INTO users (tg_id, first_seen_at) VALUES (5, ?), (6, ?)", (NOW, NOW)
        )
        await conn.execute(
            "INSERT INTO nodes (name, addr, public_host, capacity_gb) VALUES ('n1', '192.0.2.1', 'n1.example.org', 1000)"
        )
        await conn.execute(
            "INSERT INTO clients (tg_id, node_id, plan_id, quota_gb, state, expires_at, state_since, created_at)"
            " VALUES (5, 1, 'gb50', 50, 'active', ?, ?, ?)",
            (NOW + 86400, NOW, NOW),
        )
        await conn.execute(
            "INSERT INTO payments (tg_id, client_id, plan_id, stars, telegram_payment_charge_id, at)"
            " VALUES (5, 1, 'gb50', 50, 'charge-1', ?)",
            (NOW,),
        )
        await conn.execute(
            "INSERT INTO invites (code_hash, created_by, created_at, used_by, used_at)"
            " VALUES ('h-used', 900, ?, 5, ?), ('h-free', 900, ?, NULL, NULL)",
            (NOW, NOW, NOW),
        )
        await conn.execute(
            "INSERT INTO broadcasts (text, created_by, created_at, sent, failed) VALUES ('привет', 900, ?, 7, 1)",
            (NOW,),
        )
        await conn.execute(
            "INSERT INTO restore_requests (client_id, snapshot_date, status, note, created_at)"
            " VALUES (1, '2025-10-08', 'done', 'готово', ?)",
            (NOW,),
        )
        await conn.execute(
            "INSERT INTO service_events (kind, node_id, text, starts_at) VALUES ('maintenance', 1, 'работы', ?)",
            (NOW,),
        )
    yield database
    await database.close()


async def one(db: Database, sql: str, *params: object) -> tuple:  # type: ignore[type-arg]
    async with db.read() as conn, conn.execute(sql, params) as cur:
        row = await cur.fetchone()
    assert row is not None
    return tuple(row)


async def test_an_old_database_is_upgraded_to_the_latest_version(old_db: Database) -> None:
    assert await old_db.user_version() == 1
    latest = await migrate(old_db)
    assert latest == load_migrations()[-1][0] > 1
    assert await old_db.user_version() == latest


async def test_existing_data_survives_every_migration(old_db: Database) -> None:
    await migrate(old_db)
    assert await one(old_db, "SELECT COUNT(*) FROM users") == (2,)
    assert await one(
        old_db, "SELECT plan_id, quota_gb, state, expires_at FROM clients WHERE id = 1"
    ) == ("gb50", 50, "active", NOW + 86400)
    assert await one(old_db, "SELECT stars, telegram_payment_charge_id, status FROM payments") == (
        50,
        "charge-1",
        "paid",
    )
    assert await one(old_db, "SELECT used_by FROM invites WHERE code_hash = 'h-used'") == (5,)
    assert await one(old_db, "SELECT used_by FROM invites WHERE code_hash = 'h-free'") == (None,)
    assert await one(old_db, "SELECT text, sent, failed FROM broadcasts") == ("привет", 7, 1)
    assert await one(old_db, "SELECT status, note FROM restore_requests") == ("done", "готово")
    assert await one(old_db, "SELECT text FROM service_events") == ("работы",)


async def test_new_columns_get_safe_defaults_for_old_rows(old_db: Database) -> None:
    await migrate(old_db)
    assert await one(old_db, "SELECT bonus_gb, admin_note FROM clients WHERE id = 1") == (0, None)
    assert await one(old_db, "SELECT username, last_seen_at FROM users WHERE tg_id = 5") == (
        None,
        None,
    )


async def test_new_tables_exist_after_the_upgrade(old_db: Database) -> None:
    await migrate(old_db)
    names = {
        row[0] for row in await _all(old_db, "SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"admins", "audit_log", "app_settings", "media_cache"} <= names


async def test_the_upgraded_database_is_consistent(old_db: Database) -> None:
    await migrate(old_db)
    assert await one(old_db, "PRAGMA integrity_check") == ("ok",)
    assert await _all(old_db, "PRAGMA foreign_key_check") == []


async def test_running_the_migrations_twice_changes_nothing(old_db: Database) -> None:
    first = await migrate(old_db)
    before = await _all(old_db, "SELECT name, sql FROM sqlite_master ORDER BY name")
    assert await migrate(old_db) == first
    assert await _all(old_db, "SELECT name, sql FROM sqlite_master ORDER BY name") == before


async def test_an_empty_database_gets_the_same_schema_as_an_upgraded_one(
    old_db: Database, tmp_path: Path
) -> None:
    """Схема после обновления совпадает со схемой «с нуля»: расхождений между путями нет."""
    await migrate(old_db)
    fresh = await Database.open(tmp_path / "fresh.db")
    try:
        await migrate(fresh)

        async def shape(db: Database) -> list[tuple[str, tuple[str, ...]]]:
            tables = [
                row[0]
                for row in await _all(
                    db, "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
                )
            ]
            return [
                (
                    name,
                    tuple(
                        sorted(
                            f"{r[1]}:{r[2]}:{r[3]}"
                            for r in await _all(db, f"PRAGMA table_info({name})")
                        )
                    ),
                )
                for name in tables
            ]

        assert await shape(old_db) == await shape(fresh)
    finally:
        await fresh.close()


async def _all(db: Database, sql: str) -> list[tuple]:  # type: ignore[type-arg]
    async with db.read() as conn, conn.execute(sql) as cur:
        return [tuple(row) for row in await cur.fetchall()]
