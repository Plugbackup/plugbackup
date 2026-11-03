"""Нумерованные SQL-миграции: db/migrations/NNN_имя.sql.

Номер последней применённой миграции хранится в PRAGMA user_version.
"""

import logging
import re
from importlib import resources

from plugbackup_bot.db.connect import Database

log = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


def load_migrations() -> list[tuple[int, str, str]]:
    """Список (номер, имя файла, SQL) по возрастанию номера."""
    directory = resources.files("plugbackup_bot.db") / "migrations"
    found = []
    for entry in directory.iterdir():
        match = _NAME_RE.match(entry.name)
        if match:
            found.append((int(match[1]), entry.name, entry.read_text(encoding="utf-8")))
    found.sort()
    if [number for number, _, _ in found] != list(range(1, len(found) + 1)):
        raise RuntimeError("номера миграций должны идти подряд, начиная с 001")
    return found


async def migrate(db: Database) -> int:
    """Применяет недостающие миграции, возвращает текущую версию схемы."""
    migrations = load_migrations()
    latest = migrations[-1][0] if migrations else 0
    current = await db.user_version()
    if current > latest:
        raise RuntimeError(f"схема базы (v{current}) новее кода (v{latest}), обновите бота")
    for number, name, sql in migrations:
        if number <= current:
            continue
        log.info("applying migration %s", name)
        # DDL в SQLite транзакционный, user_version меняется вместе со схемой.
        await db.executescript(f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
    return latest
