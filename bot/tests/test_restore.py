import pytest
import pytest_asyncio

from fakes import FakeClock
from harness import ADMIN, ADMIN_CHAT, OTHER, USER, Harness
from helpers import add_client, add_user, fake_key_body
from plugbackup_bot import texts_ru as T
from plugbackup_bot.db import Database
from plugbackup_bot.services.restore import RestoreKind
from plugbackup_bot.tg import texts

KEY = fake_key_body(0)
HOUR = 3600
# Часы теста: 2025-10-09 11:53 по Москве.
TODAY = "2025-10-09"
OLDEST = "2025-10-03"


@pytest_asyncio.fixture
async def h(db: Database, clock: FakeClock) -> Harness:
    harness = await Harness.build(db, clock)
    await add_user(db, USER.id)
    await add_client(db, USER.id, 1, ssh_key_body=KEY)
    return harness


async def requests(h: Harness) -> list[tuple[int, str, str]]:
    async with (
        h.db.read() as conn,
        conn.execute("SELECT id, snapshot_date, status FROM restore_requests ORDER BY id") as cur,
    ):
        return [tuple(row) for row in await cur.fetchall()]


# --- сервис ---


def test_dates_cover_the_last_seven_days(h: Harness) -> None:
    dates = h.restore.available_dates()
    assert dates[0] == TODAY
    assert dates[-1] == OLDEST
    assert len(dates) == 7


async def test_request_is_created(h: Harness) -> None:
    result = await h.restore.create(USER.id, "2025-10-08")
    assert (result.kind, result.request_id) == (RestoreKind.OK, 1)
    assert result.client is not None
    assert await requests(h) == [(1, "2025-10-08", "new")]
    assert await h.restore.open_count() == 1


@pytest.mark.parametrize(
    "date", ["2025-10-02", "2025-10-10", "garbage", "2025-10-09; DROP TABLE x", "", "2025-1-9"]
)
async def test_dates_outside_the_snapshot_window_are_refused(h: Harness, date: str) -> None:
    assert (await h.restore.create(USER.id, date)).kind is RestoreKind.BAD_DATE
    assert await requests(h) == []


async def test_one_open_request_at_a_time(h: Harness) -> None:
    await h.restore.create(USER.id, TODAY)
    assert (await h.restore.create(USER.id, TODAY)).kind is RestoreKind.OPEN_EXISTS
    await h.restore.resolve(1, "done")
    assert (await h.restore.create(USER.id, TODAY)).kind is RestoreKind.OK


async def test_daily_limit(h: Harness) -> None:
    for number in range(1, 4):
        assert (await h.restore.create(USER.id, TODAY)).kind is RestoreKind.OK
        await h.restore.resolve(number, "rejected")
    assert (await h.restore.create(USER.id, TODAY)).kind is RestoreKind.TOO_MANY
    h.clock.advance(25 * HOUR)
    assert (await h.restore.create(USER.id, TODAY)).kind is RestoreKind.OK


async def test_only_working_storage_can_request(db: Database, clock: FakeClock) -> None:
    h = await Harness.build(db, clock)
    assert (await h.restore.create(USER.id, TODAY)).kind is RestoreKind.NO_CLIENT
    await add_user(db, OTHER.id)
    await add_client(db, OTHER.id, 1, state="suspended", ssh_key_body=KEY)
    assert (await h.restore.create(OTHER.id, TODAY)).kind is RestoreKind.BLOCKED
    await add_user(db, 7)
    await add_client(db, 7, 1, state="pending")
    assert (await h.restore.create(7, TODAY)).kind is RestoreKind.NO_CLIENT


async def test_resolving(h: Harness) -> None:
    await h.restore.create(USER.id, TODAY)
    resolved = await h.restore.resolve(1, "done", "восстановили")
    assert resolved is not None
    assert (resolved.status, resolved.snapshot_date, resolved.client.tg_id) == ("done", TODAY, 5)
    assert await h.restore.resolve(1, "rejected") is None  # уже обработана
    assert await h.restore.resolve(99, "done") is None
    assert await requests(h) == [(1, TODAY, "done")]
    with pytest.raises(ValueError):
        await h.restore.resolve(1, "whatever")


# --- сценарий в боте ---


async def test_restore_without_storage(db: Database, clock: FakeClock) -> None:
    h = await Harness.build(db, clock)
    await h.say("/restore")
    assert h.texts() == [T.RESTORE_NO_CLIENT]


async def test_restore_flow_from_date_to_admin_decision(h: Harness) -> None:
    await h.say("/restore")
    assert h.last().text == texts.restore_intro()
    buttons = h.last_buttons()
    assert [label for label, _, _ in buttons][:2] == ["9 октября", "8 октября"]
    assert len(buttons) == 7
    assert buttons[1][1] == "restore:d:2025-10-08"

    await h.press("restore:d:2025-10-08")
    assert "Снимок за 8 октября" in h.last().text
    assert [data for _, data, _ in h.last_buttons()] == ["restore:ok:2025-10-08", "restore:no"]

    await h.press("restore:ok:2025-10-08")
    assert h.last().text == texts.restore_accepted(1)
    [(chat, admin_text)] = [m for m in h.messenger.sent if m[0] == ADMIN_CHAT]
    assert chat == ADMIN_CHAT
    assert "Заявка №1 на восстановление" in admin_text
    assert "u1001" in admin_text
    assert "2025-10-08" in admin_text
    markup = h.messenger.markups[-1]
    assert markup is not None
    assert [b.callback_data for row in markup.inline_keyboard for b in row] == [
        "rq:done:1",
        "rq:rej:1",
    ]

    # Администратор подтверждает, клиент получает ответ.
    await h.press("rq:done:1", user=ADMIN)
    client_messages = [text for tg, text in h.messenger.sent if tg == USER.id]
    assert client_messages[-1] == texts.restore_resolved(1, "8 октября", done=True)
    assert await requests(h) == [(1, "2025-10-08", "done")]


async def test_rejection_reaches_the_client(h: Harness) -> None:
    await h.press("restore:ok:2025-10-08")
    await h.press("rq:rej:1", user=ADMIN)
    assert h.messenger.sent[-1] == (USER.id, texts.restore_resolved(1, "8 октября", done=False))


async def test_only_admins_can_resolve_requests(h: Harness) -> None:
    await h.press("restore:ok:2025-10-08")
    await h.press("rq:done:1", user=OTHER)
    await h.press("rq:done:1")  # даже сам клиент
    assert await requests(h) == [(1, "2025-10-08", "new")]


async def test_a_request_is_resolved_once(h: Harness) -> None:
    await h.press("restore:ok:2025-10-08")
    await h.press("rq:done:1", user=ADMIN)
    sent = len(h.messenger.sent)
    await h.press("rq:rej:1", user=ADMIN)
    assert h.callback_answers()[-1].text == "Заявка уже обработана."
    assert len(h.messenger.sent) == sent


async def test_the_alert_button_opens_the_same_screen(h: Harness) -> None:
    await h.press("restore:start")
    assert h.last().text == texts.restore_intro()


async def test_repeated_and_invalid_confirmations(h: Harness) -> None:
    await h.press("restore:ok:2025-10-08")
    await h.press("restore:ok:2025-10-08")
    assert h.last().text == T.RESTORE_OPEN
    await h.press("restore:d:1999-01-01")
    assert h.last().text == T.RESTORE_BAD_DATE
    await h.press("restore:no")
    assert h.last().text == T.RESTORE_CANCELLED
    assert len(await requests(h)) == 1
