import pytest
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage

from fakes import make_bot
from helpers import add_user
from plugbackup_bot.db import Database
from plugbackup_bot.services.notify import TelegramNotifier

ADMIN_CHAT = 777


def method() -> SendMessage:
    return SendMessage(chat_id=1, text="x")


async def blocked(db: Database, tg_id: int) -> bool:
    async with (
        db.read() as conn,
        conn.execute("SELECT bot_blocked FROM users WHERE tg_id = ?", (tg_id,)) as cur,
    ):
        row = await cur.fetchone()
    assert row is not None
    return bool(row[0])


async def test_message_is_sent(db: Database) -> None:
    bot, session = make_bot()
    notifier = TelegramNotifier(bot, db, ADMIN_CHAT)
    assert await notifier.send(5, "<b>привет</b>") is True
    assert [(m.chat_id, m.text) for m in session.of(SendMessage)] == [(5, "<b>привет</b>")]


async def test_blocked_user_is_remembered(db: Database) -> None:
    await add_user(db, 5)
    bot, session = make_bot()
    session.errors.append(
        TelegramForbiddenError(method(), "Forbidden: bot was blocked by the user")
    )
    notifier = TelegramNotifier(bot, db, ADMIN_CHAT)
    assert await notifier.send(5, "текст") is False
    assert await blocked(db, 5) is True


async def test_flood_wait_is_retried_once(db: Database) -> None:
    bot, session = make_bot()
    notifier = TelegramNotifier(bot, db, ADMIN_CHAT)
    session.errors.append(TelegramRetryAfter(method(), "Too Many Requests", retry_after=0))
    assert await notifier.send(5, "текст") is True
    assert len(session.requests) == 2

    session.requests.clear()
    session.errors.extend([TelegramRetryAfter(method(), "Too Many Requests", retry_after=0)] * 2)
    assert await notifier.send(5, "текст") is False
    assert len(session.requests) == 2


async def test_other_api_errors_do_not_raise(db: Database) -> None:
    bot, session = make_bot()
    session.errors.append(TelegramAPIError(method(), "Bad Request: chat not found"))
    assert await TelegramNotifier(bot, db, ADMIN_CHAT).send(5, "текст") is False


async def test_admin_alert_is_plain_text_and_bounded(db: Database) -> None:
    bot, session = make_bot()
    notifier = TelegramNotifier(bot, db, ADMIN_CHAT)
    await notifier.alert("ошибка <script> & " + "x" * 10_000)
    [sent] = session.of(SendMessage)
    assert sent.chat_id == ADMIN_CHAT
    assert "&lt;script&gt; &amp;" in sent.text
    assert len(sent.text) < 4096


async def test_alert_never_raises(db: Database) -> None:
    bot, session = make_bot()
    session.errors.append(TelegramAPIError(method(), "Bad Request: chat not found"))
    await TelegramNotifier(bot, db, ADMIN_CHAT).alert("текст")


@pytest.mark.parametrize("text", ["a", "б" * 50])
async def test_alert_keeps_text(db: Database, text: str) -> None:
    bot, session = make_bot()
    await TelegramNotifier(bot, db, ADMIN_CHAT).alert(text)
    assert session.texts() == [text]
