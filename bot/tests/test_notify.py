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


async def test_flood_wait_is_retried_a_few_times_then_given_up(db: Database) -> None:
    bot, session = make_bot()
    notifier = TelegramNotifier(bot, db, ADMIN_CHAT)
    session.errors.append(TelegramRetryAfter(method(), "Too Many Requests", retry_after=0))
    assert await notifier.send(5, "текст") is True
    assert len(session.requests) == 2

    session.requests.clear()
    session.errors.extend([TelegramRetryAfter(method(), "Too Many Requests", retry_after=0)] * 3)
    assert await notifier.send(5, "текст") is False
    assert len(session.requests) == 3  # три попытки, дальше отказ


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


async def test_the_effect_is_added_only_when_asked_for_and_enabled(db: Database) -> None:
    from plugbackup_bot.ui.effects import EFFECT_CONFETTI, Effects

    bot, session = make_bot()
    notifier = TelegramNotifier(bot, db, ADMIN_CHAT, Effects())
    await notifier.send(5, "с эффектом", effect=True)
    await notifier.send(5, "без эффекта")
    with_effect, without = session.of(SendMessage)
    assert with_effect.message_effect_id == EFFECT_CONFETTI and without.message_effect_id is None

    silent_bot, silent_session = make_bot()
    off = TelegramNotifier(silent_bot, db, ADMIN_CHAT, Effects(enabled=False))
    await off.send(5, "эффекты выключены", effect=True)
    assert silent_session.of(SendMessage)[0].message_effect_id is None

    plain_bot, plain_session = make_bot()  # без Effects вообще: как раньше
    await TelegramNotifier(plain_bot, db, ADMIN_CHAT).send(5, "x", effect=True)
    assert plain_session.of(SendMessage)[0].message_effect_id is None


async def test_delivery_tells_blocked_from_failed(db: Database) -> None:
    from plugbackup_bot.services.notify import Delivery

    await add_user(db, 5)
    bot, session = make_bot()
    notifier = TelegramNotifier(bot, db, ADMIN_CHAT)
    assert await notifier.deliver(5, "x") is Delivery.SENT
    session.errors.append(
        TelegramForbiddenError(method(), "Forbidden: bot was blocked by the user")
    )
    assert await notifier.deliver(5, "x") is Delivery.BLOCKED
    assert await blocked(db, 5) is True
    session.errors.append(TelegramAPIError(method(), "Bad Request: chat not found"))
    assert await notifier.deliver(6, "x") is Delivery.FAILED
