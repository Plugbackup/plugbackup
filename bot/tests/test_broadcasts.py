"""Рассылки: сегменты, скорость, статистика, продолжение после перезапуска, отложенная отправка,
отмена, кнопки и админский сценарий с проверкой прав."""

import asyncio
import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from aiogram.methods import AnswerCallbackQuery

from fakes import FakeClock, RecordingMessenger
from harness import ADMIN, SUPPORT, USER, Harness
from helpers import CATALOG, NOW, add_client, add_node, add_user, plain
from plugbackup_bot import texts_ru as T
from plugbackup_bot.db import Database
from plugbackup_bot.db import audit as audit_repo
from plugbackup_bot.domain.roles import Actor, Role
from plugbackup_bot.services.broadcast import (
    MAX_SCHEDULE_DAYS,
    MAX_TEXT,
    BroadcastService,
    BroadcastStatus,
    ButtonProblem,
    Segment,
    parse_buttons,
    parse_send_time,
)
from plugbackup_bot.tg.admin import core
from plugbackup_bot.tg.admin.core import cb

OWNER = Actor(ADMIN.id, Role.OWNER)
B = core.BROADCASTS
DAY = 24 * 3600
TZ = "Europe/Moscow"


def service(db: Database, clock: FakeClock, messenger: RecordingMessenger, **kwargs: object):  # type: ignore[no-untyped-def]
    kwargs.setdefault("rate_per_s", 1000)
    return BroadcastService(db, clock, messenger, CATALOG, **kwargs)  # type: ignore[arg-type]


async def fill_segments(db: Database) -> None:
    """Клиенты на разных тарифах и в разных состояниях, не клиенты и заблокировавший бота."""
    await add_node(db)
    for tg_id in range(1, 9):
        await add_user(db, tg_id)
    await add_client(db, 1, 1, plan_id="beta", quota_gb=10)  # бета
    await add_client(db, 2, 1, plan_id="gb50")  # платный
    await add_client(db, 3, 1, plan_id="gb100", quota_gb=100)  # платный, скоро истекает
    await add_client(db, 4, 1, state="suspended")  # заблокирован
    await add_client(db, 5, 1, plan_id="gb50")  # платный, но заблокировал бота
    async with db.transaction() as conn:
        await conn.execute(
            "UPDATE clients SET expires_at = ? WHERE tg_id = 3", (NOW + 3 * DAY,)
        )  # через 3 дня
        await conn.execute("UPDATE clients SET expires_at = ? WHERE tg_id = 2", (NOW + 30 * DAY,))
        await conn.execute("UPDATE users SET bot_blocked = 1 WHERE tg_id = 5")
        await conn.execute(
            "UPDATE clients SET confirm_due_at = ? WHERE tg_id = 1", (NOW + 2 * DAY,)
        )


# --- получатели ---


async def test_segments_pick_the_right_people(db: Database, clock: FakeClock) -> None:
    await fill_segments(db)
    svc = service(db, clock, RecordingMessenger())
    assert await svc.recipients(Segment.ALL) == [1, 2, 3, 4, 6, 7, 8]  # без заблокировавшего бота
    assert await svc.recipients(Segment.BETA) == [1]
    assert await svc.recipients(Segment.PAID) == [2, 3]  # 4 приостановлен, 5 недоступен
    assert await svc.recipients(Segment.EXPIRING) == [
        1,
        3,
    ]  # бета по подтверждению, платный по сроку
    assert await svc.recipients(Segment.BLOCKED) == [4]
    assert await svc.recipients("node:1") == [1, 2, 3, 4]
    assert await svc.recipients("node:99") == []
    counts = await svc.segment_counts()
    assert counts == {"all": 7, "beta": 1, "paid": 2, "expiring": 2, "blocked": 1}


async def test_an_unknown_segment_is_refused(db: Database, clock: FakeClock) -> None:
    svc = service(db, clock, RecordingMessenger())
    with pytest.raises(ValueError):
        await svc.create(OWNER, segment="everyone", text="Привет")
    with pytest.raises(ValueError):
        await svc.create(OWNER, segment="node:abc", text="Привет")


# --- отправка и статистика ---


async def test_sending_counts_delivered_blocked_and_failed(db: Database, clock: FakeClock) -> None:
    for tg_id in (1, 2, 3, 4):
        await add_user(db, tg_id)
    messenger = RecordingMessenger()
    messenger.blocked, messenger.failing = {2}, {3}
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    svc = service(db, clock, messenger, rate_per_s=20, sleep=sleep)
    broadcast_id = await svc.create(OWNER, segment=Segment.ALL, text="Привет <всем> & каждому")
    assert await svc.run_due() == 2  # доставлено двоим
    item = await svc.get(broadcast_id)
    assert item is not None and item.status is BroadcastStatus.DONE
    assert (item.total, item.sent, item.blocked, item.failed, item.pending) == (4, 2, 1, 1, 0)
    assert [tg_id for tg_id, _ in messenger.sent] == [1, 4]
    assert messenger.sent[0][1] == "Привет &lt;всем&gt; &amp; каждому"  # текст экранирован
    assert delays == [0.05] * 4  # 20 сообщений в секунду
    assert await svc.run_due() == 0  # повторно никому не уходит


async def test_the_default_rate_stays_under_the_telegram_limit(
    db: Database, clock: FakeClock
) -> None:
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    await add_user(db, 1)
    svc = BroadcastService(db, clock, RecordingMessenger(), CATALOG, sleep=sleep)
    await svc.create(OWNER, segment=Segment.ALL, text="Привет")
    await svc.run_due()
    assert delays and delays[0] >= 1 / 30  # Telegram разрешает около 30 сообщений в секунду


async def test_a_crash_while_sending_resumes_without_duplicates(
    db: Database, clock: FakeClock
) -> None:
    for tg_id in (1, 2, 3, 4, 5):
        await add_user(db, tg_id)

    class Dies(RecordingMessenger):
        async def deliver(self, tg_id, text, *, reply_markup=None):  # type: ignore[no-untyped-def,override]
            if tg_id == 3:
                raise asyncio.CancelledError  # процесс остановили посреди рассылки
            return await super().deliver(tg_id, text, reply_markup=reply_markup)

    first = Dies()
    svc = service(db, clock, first)
    broadcast_id = await svc.create(OWNER, segment=Segment.ALL, text="Привет")
    with pytest.raises(asyncio.CancelledError):
        await svc.run_due()
    item = await svc.get(broadcast_id)
    assert item is not None and item.status is BroadcastStatus.SENDING
    assert (item.sent, item.pending) == (2, 3)

    # Новый запуск бота: получатели не пересчитываются и первым двоим повторно не пишут.
    await add_user(db, 6)  # пришёл уже после начала рассылки: её не получит
    second = RecordingMessenger()
    assert await service(db, clock, second).run_due() == 3
    assert [tg_id for tg_id, _ in second.sent] == [3, 4, 5]
    item = await svc.get(broadcast_id)
    assert item is not None and item.status is BroadcastStatus.DONE
    assert (item.total, item.sent) == (5, 5)


async def test_an_unexpected_error_marks_one_recipient_and_goes_on(
    db: Database, clock: FakeClock
) -> None:
    for tg_id in (1, 2, 3):
        await add_user(db, tg_id)

    class Breaks(RecordingMessenger):
        async def deliver(self, tg_id, text, *, reply_markup=None):  # type: ignore[no-untyped-def,override]
            if tg_id == 2:
                raise RuntimeError("telegram")
            return await super().deliver(tg_id, text, reply_markup=reply_markup)

    svc = service(db, clock, Breaks())
    broadcast_id = await svc.create(OWNER, segment=Segment.ALL, text="Привет")
    assert await svc.run_due() == 2
    item = await svc.get(broadcast_id)
    assert item is not None and (item.sent, item.failed) == (2, 1)


async def test_recipients_are_fixed_when_sending_starts(db: Database, clock: FakeClock) -> None:
    await add_user(db, 1)
    messenger = RecordingMessenger()
    svc = service(db, clock, messenger)
    broadcast_id = await svc.create(OWNER, segment=Segment.ALL, text="Привет")
    await add_user(db, 2)  # появился до начала отправки: получит
    await svc.run_due()
    assert [tg_id for tg_id, _ in messenger.sent] == [1, 2]
    item = await svc.get(broadcast_id)
    assert item is not None and item.total == 2


# --- расписание и отмена ---


async def test_a_scheduled_broadcast_waits_for_its_time(db: Database, clock: FakeClock) -> None:
    await add_user(db, 1)
    messenger = RecordingMessenger()
    svc = service(db, clock, messenger)
    when = clock.now() + 3600
    broadcast_id = await svc.create(OWNER, segment=Segment.ALL, text="Позже", send_at=when)
    assert await svc.run_due() == 0
    assert messenger.sent == []
    item = await svc.get(broadcast_id)
    assert item is not None and item.status is BroadcastStatus.SCHEDULED and item.send_at == when
    clock.advance(3600)
    assert await svc.run_due() == 1
    assert messenger.sent == [(1, "Позже")]


async def test_the_time_must_be_in_the_future_and_not_too_far(
    db: Database, clock: FakeClock
) -> None:
    svc = service(db, clock, RecordingMessenger())
    for bad in (clock.now() - 1, clock.now(), clock.now() + (MAX_SCHEDULE_DAYS + 1) * DAY):
        with pytest.raises(ValueError):
            await svc.create(OWNER, segment=Segment.ALL, text="Привет", send_at=bad)
    await svc.create(OWNER, segment=Segment.ALL, text="Привет", send_at=clock.now() + 60)


async def test_a_cancelled_broadcast_is_not_sent(db: Database, clock: FakeClock) -> None:
    await add_user(db, 1)
    messenger = RecordingMessenger()
    svc = service(db, clock, messenger)
    broadcast_id = await svc.create(
        OWNER, segment=Segment.ALL, text="Передумал", send_at=clock.now() + 60
    )
    assert await svc.cancel(broadcast_id, OWNER) is True
    assert await svc.cancel(broadcast_id, OWNER) is False  # второй раз уже поздно
    clock.advance(120)
    assert await svc.run_due() == 0
    item = await svc.get(broadcast_id)
    assert item is not None and item.status is BroadcastStatus.CANCELLED


async def test_cancelling_a_running_broadcast_stops_it(db: Database, clock: FakeClock) -> None:
    for tg_id in range(1, 31):
        await add_user(db, tg_id)

    class Stops(RecordingMessenger):
        async def deliver(self, tg_id, text, *, reply_markup=None):  # type: ignore[no-untyped-def,override]
            outcome = await super().deliver(tg_id, text, reply_markup=reply_markup)
            if tg_id == 2:
                await svc.cancel(broadcast_id, OWNER)
            return outcome

    messenger = Stops()
    svc = service(db, clock, messenger)
    broadcast_id = await svc.create(OWNER, segment=Segment.ALL, text="Стоп")
    await svc.run_due()
    item = await svc.get(broadcast_id)
    assert item is not None and item.status is BroadcastStatus.CANCELLED
    # Начатая партия (25 получателей) дописывается до конца, следующая уже не начинается.
    assert (item.sent, item.pending) == (25, 5)
    assert await svc.open_count() == 0


async def test_text_and_buttons_are_validated(db: Database, clock: FakeClock) -> None:
    svc = service(db, clock, RecordingMessenger())
    for bad in ("", "   ", "x" * (MAX_TEXT + 1)):
        with pytest.raises(ValueError):
            await svc.create(OWNER, segment=Segment.ALL, text=bad)
    with pytest.raises(ValueError):
        await svc.create(
            OWNER, segment=Segment.ALL, text="Привет", buttons=[("a", "https://x.y")] * 4
        )


def test_button_lines_are_parsed() -> None:
    assert parse_buttons("") == ()
    assert parse_buttons("Сайт | https://example.org\nКанал | tg://resolve?domain=example") == (
        ("Сайт", "https://example.org"),
        ("Канал", "tg://resolve?domain=example"),
    )
    assert parse_buttons("Вопрос | да | https://example.org") == (
        ("Вопрос | да", "https://example.org"),
    )  # подпись может содержать «|»: адрес берётся с конца


@pytest.mark.parametrize(
    "raw",
    [
        "без адреса",
        "Пусто |",
        "| https://example.org",
        "Файл | file:///etc/passwd",
        "Скрипт | javascript:alert(1)",
        "Пробел | https://exa mple.org",
        "Длинная подпись " + "я" * 40 + " | https://example.org",
        "\n".join(f"К{i} | https://example.org/{i}" for i in range(4)),
    ],
)
def test_bad_button_lines_are_refused(raw: str) -> None:
    with pytest.raises(ButtonProblem):
        parse_buttons(raw)


async def test_buttons_reach_the_messenger(db: Database, clock: FakeClock) -> None:
    await add_user(db, 1)
    messenger = RecordingMessenger()
    svc = service(db, clock, messenger)
    await svc.create(
        OWNER,
        segment=Segment.ALL,
        text="Привет",
        buttons=parse_buttons("Сайт | https://example.org"),
    )
    await svc.run_due()
    markup = messenger.markups[0]
    assert markup is not None
    [[button]] = markup.inline_keyboard
    assert (button.text, button.url) == ("Сайт", "https://example.org")


def test_send_times_are_read_in_the_bot_time_zone() -> None:
    now = int(datetime(2025, 10, 9, 8, 53, tzinfo=UTC).timestamp())  # в Москве 11:53
    expected = int(datetime(2025, 10, 12, 6, 30, tzinfo=UTC).timestamp())  # 09:30 по Москве
    assert parse_send_time("12.10 09:30", TZ, now) == expected
    assert parse_send_time("12.10.2025 09:30", TZ, now) == expected
    assert parse_send_time("  12.10   09:30 ", TZ, now) == expected
    # Дата без года, которая в этом году уже прошла, переносится на следующий год.
    december = int(datetime(2025, 12, 30, 12, 0, tzinfo=UTC).timestamp())
    january = int(datetime(2026, 1, 2, 7, 0, tzinfo=UTC).timestamp())
    assert parse_send_time("02.01 10:00", TZ, december) == january
    for bad in ("", "завтра", "32.10 10:00", "12.10 25:00", "12.10", "01.01.2020 10:00"):
        with pytest.raises(ValueError):
            parse_send_time(bad, TZ, now)
    with pytest.raises(ValueError):
        parse_send_time("12.12 10:00", TZ, now)  # дальше 30 дней


# --- журнал ---


async def test_creating_and_cancelling_are_logged_without_the_text(
    db: Database, clock: FakeClock
) -> None:
    svc = service(db, clock, RecordingMessenger())
    broadcast_id = await svc.create(OWNER, segment=Segment.PAID, text="Секретный текст акции")
    await svc.cancel(broadcast_id, OWNER)
    async with db.read() as conn:
        entries = await audit_repo.page(conn, limit=10, offset=0)
    actions = [entry.action for entry in entries]
    assert "broadcast.create" in actions and "broadcast.cancel" in actions
    assert all(
        "Секретный" not in json.dumps(entry.details, ensure_ascii=False) for entry in entries
    )


# --- в админке ---


@pytest_asyncio.fixture
async def h(db: Database, clock: FakeClock) -> Harness:
    harness = await Harness.build(db, clock)
    await harness.directory.add_support(OWNER, SUPPORT.id)
    for tg_id in (USER.id, 11, 12):
        await add_user(db, tg_id)
    async with db.transaction() as conn:
        await conn.execute("DELETE FROM audit_log")
    return harness


def shown(h: Harness) -> list[str]:
    """Подписи кнопок последнего экрана без «Главного меню» админки."""
    return [text for text, _, _ in h.last_buttons() if text != T.BTN_ADM_MENU]


def answers(h: Harness) -> list[str | None]:
    return [a.text for a in h.session.of(AnswerCallbackQuery)]


async def test_the_menu_button_opens_the_broadcast_list(h: Harness) -> None:
    await h.say("/admin", user=ADMIN)
    assert any(data == cb(B) for _, data, _ in h.last_buttons())
    await h.press(cb(B), user=ADMIN)
    assert T.BC_EMPTY in plain(h.last().text)


async def test_support_cannot_use_broadcasts(h: Harness) -> None:
    await h.say("/admin", user=SUPPORT)
    assert all(data != cb(B) for _, data, _ in h.last_buttons())  # кнопки нет
    h.session.requests.clear()
    for data in (cb(B), cb(B, "list"), cb(B, "new"), cb(B, "go"), cb(B, "cx!", i=1)):
        await h.press(data, user=SUPPORT)
    assert h.session.outputs() == []
    assert answers(h).count(T.ADM_DENIED) == 5
    await h.say("/broadcast", user=SUPPORT)
    assert h.last().text == T.ADM_DENIED
    assert await h.broadcast.open_count() == 0


async def test_a_broadcast_goes_through_the_whole_dialogue(h: Harness) -> None:
    await h.press(cb(B, "new"), user=ADMIN)
    assert any(text.startswith("Все (4)") for text in shown(h))  # трое в базе и сам админ
    assert any(text.startswith("Бета (0)") for text in shown(h))

    await h.press(cb(B, "seg", x="all"), user=ADMIN)
    assert await h.fsm_state(ADMIN) == "BroadcastStates:text"
    await h.say("Привет, <b>друзья</b>", user=ADMIN)
    assert await h.fsm_state(ADMIN) == "BroadcastStates:buttons"

    await h.say("просто слова", user=ADMIN)  # строка кнопки без адреса
    assert "Подпись | https://адрес" in plain(h.last().text)
    await h.say("Сайт | https://example.org", user=ADMIN)
    assert shown(h) == [T.BTN_BC_NOW, T.BTN_BC_LATER]

    await h.press(cb(B, "now"), user=ADMIN)
    preview = plain(h.last().text)
    assert "Получателей сейчас: 4" in preview and "Сайт → https://example.org" in preview
    assert "<b>друзья</b>" in plain(h.last().text)  # разметка показана текстом, не применена
    assert "&lt;b&gt;" in h.last().text
    send = [
        b
        for row in h.last().reply_markup.inline_keyboard
        for b in row
        if b.callback_data == cb(B, "go")
    ]
    assert send and send[0].style == "danger"  # красная кнопка и есть подтверждение

    h.messenger.sent.clear()
    await h.press(cb(B, "test"), user=ADMIN)  # пробное сообщение только админу
    assert [tg_id for tg_id, _ in h.messenger.sent] == [ADMIN.id]
    assert answers(h)[-1] == T.BC_TEST_SENT

    h.messenger.sent.clear()
    await h.press(cb(B, "go"), user=ADMIN)
    assert "Рассылка №1 принята" in plain(h.last().text)
    assert h.broadcast_kicks == [1]
    assert await h.fsm_state(ADMIN) is None
    await h.broadcast.run_due()
    assert sorted(tg_id for tg_id, _ in h.messenger.sent) == [USER.id, 11, 12, ADMIN.id]
    assert h.messenger.markups[0] is not None

    await h.press(cb(B, "card", i=1), user=ADMIN)
    card = plain(h.last().text)
    assert "Доставлено: 4" in card and "Заблокировали бота: 0" in card and "завершена" in card
    await h.press(cb(B, "list"), user=ADMIN)
    # Рассылка видна кнопкой списка (знак «№» тест вместе со значком отбрасывает).
    assert any(text.startswith("1 · Все · готово 4/4") for text in shown(h))


async def test_a_scheduled_broadcast_from_the_dialogue(h: Harness) -> None:
    await h.press(cb(B, "seg", x="all"), user=ADMIN)
    await h.say("Анонс", user=ADMIN)
    await h.press(cb(B, "nb"), user=ADMIN)
    await h.press(cb(B, "later"), user=ADMIN)
    assert await h.fsm_state(ADMIN) == "BroadcastStates:when"
    await h.say("когда-нибудь", user=ADMIN)
    assert T.BC_BAD_DATETIME in plain(h.last().text)
    assert await h.fsm_state(ADMIN) == "BroadcastStates:when"

    soon = datetime.fromtimestamp(h.clock.now() + 2 * DAY, ZoneInfo(TZ)).strftime("%d.%m %H:%M")
    await h.say(soon, user=ADMIN)
    assert T.BTN_BC_SCHEDULE in shown(h)
    await h.press(cb(B, "go"), user=ADMIN)
    assert "запланирована" in plain(h.last().text)
    assert h.broadcast_kicks == []  # сразу ничего не уходит
    assert await h.broadcast.run_due() == 0

    h.clock.advance(3 * DAY)
    assert await h.broadcast.run_due() == 4


async def test_cancelling_from_the_card_asks_for_confirmation(h: Harness) -> None:
    broadcast_id = await h.broadcast.create(
        OWNER, segment=Segment.ALL, text="Отмена", send_at=h.clock.now() + 600
    )
    await h.press(cb(B, "card", i=broadcast_id), user=ADMIN)
    assert "ожидает отправки" in plain(h.last().text)
    await h.press(cb(B, "cx", i=broadcast_id), user=ADMIN)
    assert T.ADM_CONFIRM_TITLE in plain(h.last().text)
    item = await h.broadcast.get(broadcast_id)
    assert item is not None and item.status is BroadcastStatus.SCHEDULED  # пока не отменена
    await h.press(cb(B, "cx!", i=broadcast_id), user=ADMIN)
    assert "отменена" in plain(h.last().text)
    item = await h.broadcast.get(broadcast_id)
    assert item is not None and item.status is BroadcastStatus.CANCELLED
    await h.press(cb(B, "cx!", i=broadcast_id), user=ADMIN)  # повторное нажатие
    assert T.BC_CANCEL_TOO_LATE in plain(h.last().text)


async def test_a_lost_draft_does_not_crash(h: Harness) -> None:
    await h.press(cb(B, "go"), user=ADMIN)  # состояние потеряно, например после перезапуска
    assert T.BC_LOST in plain(h.last().text)
    await h.press(cb(B, "test"), user=ADMIN)
    assert answers(h)[-1] == T.BC_LOST
    assert await h.broadcast.open_count() == 0


async def test_cancel_leaves_the_dialogue(h: Harness) -> None:
    await h.press(cb(B, "seg", x="all"), user=ADMIN)
    await h.say("/cancel", user=ADMIN)
    assert await h.fsm_state(ADMIN) is None
    await h.say("Это обычный текст", user=ADMIN)
    assert await h.broadcast.open_count() == 0


async def test_an_empty_segment_cannot_be_sent(h: Harness) -> None:
    await h.press(cb(B, "seg", x="blocked"), user=ADMIN)
    await h.say("Тест", user=ADMIN)
    await h.press(cb(B, "nb"), user=ADMIN)
    await h.press(cb(B, "now"), user=ADMIN)
    assert T.BC_PREVIEW_NOBODY in plain(h.last().text)
    assert all(data != cb(B, "go") for _, data, _ in h.last_buttons())


async def test_a_forged_segment_is_refused(h: Harness) -> None:
    await h.press(cb(B, "seg", x="everyone"), user=ADMIN)
    assert T.ADM_STALE in plain(h.last().text)
    assert await h.fsm_state(ADMIN) is None
