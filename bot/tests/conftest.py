from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from fakes import FakeClock
from plugbackup_bot.db import Database, migrate


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = await Database.open(tmp_path / "bot.db")
    await migrate(database)
    try:
        yield database
    finally:
        await database.close()
