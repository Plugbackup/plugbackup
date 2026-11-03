import pytest

from helpers import NOW, add_user
from plugbackup_bot.db import Database
from plugbackup_bot.db.invites import (
    RedeemStatus,
    code_hash,
    count_unused,
    create_invites,
    new_code,
    normalize,
    redeem,
)


def test_generated_codes_are_well_formed() -> None:
    codes = {new_code() for _ in range(200)}
    assert len(codes) == 200
    for code in codes:
        assert len(code) == 11
        assert code[5] == "-"
        assert normalize(code) == code.replace("-", "")


@pytest.mark.parametrize("text", ["abcde-fghjk", " ABCDE FGHJK ", "abcdefghjk", "ABCDE-FGHJK"])
def test_user_input_is_normalised(text: str) -> None:
    assert normalize(text) == "ABCDEFGHJK"


@pytest.mark.parametrize(
    "text",
    ["", "ABCDE", "ABCDE-FGHJK-M", "ABCDE-FGHJ0", "ABCDE-FGHJI", "ABCDE-FGHJ;", "ёёёёё-ёёёёё"],
)
def test_bad_input_is_rejected(text: str) -> None:
    assert normalize(text) is None


async def test_codes_are_stored_only_as_hashes(db: Database) -> None:
    async with db.transaction() as conn:
        codes = await create_invites(conn, 3, 1, NOW)
    async with db.read() as conn, conn.execute("SELECT code_hash FROM invites") as cur:
        stored = {row[0] for row in await cur.fetchall()}
    assert stored == {code_hash(normalize(code) or "") for code in codes}
    assert all(code.replace("-", "") not in value for code in codes for value in stored)


async def test_code_works_once(db: Database) -> None:
    await add_user(db, 1)
    await add_user(db, 2)
    async with db.transaction() as conn:
        [code] = await create_invites(conn, 1, 9, NOW)
    async with db.transaction() as conn:
        assert await count_unused(conn) == 1
        assert await redeem(conn, code.lower(), 1, NOW) is RedeemStatus.OK
        assert await count_unused(conn) == 0
        assert await redeem(conn, code, 2, NOW) is RedeemStatus.USED
        assert await redeem(conn, "AAAAA-AAAAA", 2, NOW) is RedeemStatus.INVALID
        assert await redeem(conn, "nonsense", 2, NOW) is RedeemStatus.INVALID
    async with db.read() as conn, conn.execute("SELECT used_by, used_at FROM invites") as cur:
        row = await cur.fetchone()
    assert row is not None
    assert (row[0], row[1]) == (1, NOW)
