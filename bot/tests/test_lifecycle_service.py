import pytest_asyncio

from fakes import FakeClock
from harness import USER, Harness
from helpers import NOW, add_client, add_user, fake_key_body
from plugbackup_bot.db import Database
from plugbackup_bot.db import jobs as jobs_repo
from plugbackup_bot.db.clients import Client, get_client, transition
from plugbackup_bot.db.jobs import JobStatus, Op
from plugbackup_bot.domain.lifecycle import Notice, NoticeKind, Step
from plugbackup_bot.domain.policy import DAY
from plugbackup_bot.domain.states import State
from plugbackup_bot.services.lifecycle import AliveKind, LifecycleService, TickReport
from plugbackup_bot.tg import keyboards, texts
from plugbackup_bot.tg.notices import TelegramNoticeRenderer

KEY = fake_key_body(0)


@pytest_asyncio.fixture
async def h(db: Database, clock: FakeClock) -> Harness:
    return await Harness.build(db, clock)


async def make_client(
    h: Harness,
    state: State,
    *,
    plan: str = "gb50",
    tg_id: int = USER.id,
    key: str | None = KEY,
    **columns: object,
) -> Client:
    await add_user(h.db, tg_id)
    node_id = 1
    client_id = await add_client(
        h.db,
        tg_id,
        node_id,
        state=state.value,
        plan_id=plan,
        quota_gb=10 if plan == "beta" else 50,
        ssh_key_body=key,
    )
    if columns:
        assignments = ", ".join(f"{name} = ?" for name in columns)
        async with h.db.transaction() as conn:
            await conn.execute(
                f"UPDATE clients SET {assignments} WHERE id = ?",  # noqa: S608
                (*columns.values(), client_id),
            )
    return await fetch(h, client_id)


async def fetch(h: Harness, client_id: int) -> Client:
    async with h.db.read() as conn:
        client = await get_client(conn, client_id)
    assert client is not None
    return client


def messages(h: Harness) -> list[str]:
    return [text for _, text in h.messenger.sent]


async def open_jobs(h: Harness, client_id: int) -> list[tuple[Op, JobStatus]]:
    async with h.db.read() as conn:
        return [(j.op, j.status) for j in await jobs_repo.list_for_client(conn, client_id)]


# --- бета от вопроса до удаления ---


async def test_beta_timeline(h: Harness) -> None:
    client = await make_client(
        h, State.ACTIVE, plan="beta", confirm_due_at=NOW + 30 * DAY, quota_gb=10
    )

    # До срока ничего не происходит.
    h.clock.advance(30 * DAY - 1)
    assert (await h.lifecycle.tick()).transitions == {}
    assert h.messenger.sent == []

    # День 30: вопрос с кнопкой.
    h.clock.advance(1)
    report = await h.lifecycle.tick()
    assert report.transitions == {"expiring": 1}
    assert (await fetch(h, client.id)).state is State.EXPIRING
    text = messages(h)[-1]
    assert text.startswith("Вы ещё пользуетесь хранилищем?")
    assert h.messenger.sent[-1][0] == USER.id

    # Повторный проход в ту же секунду ничего не шлёт.
    await h.lifecycle.tick()
    assert len(h.messenger.sent) == 1

    h.clock.advance(4 * DAY)  # осталось 3 дня
    await h.lifecycle.tick()
    assert messages(h)[-1].startswith("Напоминание: нажмите «Я ещё пользуюсь»")

    h.clock.advance(2 * DAY)  # остался 1 день
    await h.lifecycle.tick()
    assert messages(h)[-1].startswith("Завтра")
    assert "будет заблокировано" in messages(h)[-1]

    # День 37: блокировка и задача suspend.
    h.clock.advance(DAY)
    await h.lifecycle.tick()
    assert (await fetch(h, client.id)).state is State.SUSPENDED
    assert messages(h)[-1].startswith("Хранилище заблокировано")
    assert await open_jobs(h, client.id) == [(Op.SUSPEND, JobStatus.PENDING)]
    await h.provisioner.run_due()
    assert h.storectl.calls == [("suspend", "192.0.2.10", client.login)]

    # Через 13 дней — предупреждение, через 14 — удаление.
    h.clock.advance(13 * DAY)
    await h.lifecycle.tick()
    assert "будут удалены без возможности восстановления" in messages(h)[-1]
    h.clock.advance(DAY)
    await h.lifecycle.tick()
    assert (await fetch(h, client.id)).state is State.DELETED
    assert messages(h)[-1].startswith("Хранилище удалено")
    await h.provisioner.run_due()
    assert [call[0] for call in h.storectl.calls] == ["suspend", "delete"]

    # Дальше тишина.
    sent = len(h.messenger.sent)
    h.clock.advance(100 * DAY)
    await h.lifecycle.tick()
    assert len(h.messenger.sent) == sent


async def test_the_question_has_a_working_button(h: Harness) -> None:
    client = await make_client(h, State.ACTIVE, plan="beta", confirm_due_at=NOW, quota_gb=10)
    await h.lifecycle.tick()
    renderer = TelegramNoticeRenderer("Europe/Moscow")
    _, markup = renderer.render(Notice(NoticeKind.BETA_PROMPT, "1", True, at=NOW), client)
    assert markup is not None
    assert markup.inline_keyboard[0][0].callback_data == f"alive:{client.id}"
    assert markup.inline_keyboard[0][0].text == texts.BTN_ALIVE


# --- кнопка «Я ещё пользуюсь» ---


async def test_alive_button_restores_an_expiring_client(h: Harness) -> None:
    client = await make_client(h, State.ACTIVE, plan="beta", confirm_due_at=NOW, quota_gb=10)
    await h.lifecycle.tick()
    h.clock.advance(2 * DAY)
    await h.press(f"alive:{client.id}")

    restored = await fetch(h, client.id)
    assert restored.state is State.ACTIVE
    assert restored.confirm_due_at == h.clock.now() + 30 * DAY
    assert h.last().text.startswith("Спасибо. Следующий вопрос —")
    assert await open_jobs(h, client.id) == []

    # Новый вопрос придёт только через 30 дней.
    h.clock.advance(29 * DAY)
    assert (await h.lifecycle.tick()).transitions == {}


async def test_alive_button_unblocks_a_suspended_client(h: Harness) -> None:
    client = await make_client(h, State.SUSPENDED, plan="beta", quota_gb=10)
    result = await h.lifecycle.confirm_alive(USER.id, client.id)
    assert (result.kind, result.resumed) == (AliveKind.OK, True)
    assert (await fetch(h, client.id)).state is State.ACTIVE
    assert await open_jobs(h, client.id) == [(Op.RESUME, JobStatus.PENDING)]


async def test_alive_button_refusals(h: Harness) -> None:
    beta = await make_client(h, State.SUSPENDED, plan="beta", quota_gb=10, admin_hold=1)
    assert (await h.lifecycle.confirm_alive(USER.id, beta.id)).kind is AliveKind.BLOCKED
    assert (await h.lifecycle.confirm_alive(USER.id, beta.id + 9)).kind is AliveKind.NO_CLIENT
    assert (await h.lifecycle.confirm_alive(999, beta.id)).kind is AliveKind.NO_CLIENT

    await h.press(f"alive:{beta.id}")
    assert h.last().text == texts.ALIVE_BLOCKED
    await h.press("alive:abc")
    assert h.last().text == texts.ALIVE_NO_CLIENT


async def test_alive_button_is_for_beta_only(h: Harness) -> None:
    paid = await make_client(h, State.ACTIVE, expires_at=NOW + 5 * DAY)
    assert (await h.lifecycle.confirm_alive(USER.id, paid.id)).kind is AliveKind.NOT_BETA


# --- платные клиенты ---


async def test_cancelled_subscription_warns_then_blocks(h: Harness) -> None:
    end = NOW + 10 * DAY
    client = await make_client(h, State.ACTIVE, expires_at=end, renewal_on=0)

    h.clock.advance(7 * DAY)  # осталось 3 дня
    await h.lifecycle.tick()
    assert (await fetch(h, client.id)).state is State.EXPIRING
    assert messages(h)[-1].startswith("Доступ закончится")
    assert "продление отключено" in messages(h)[-1]

    h.clock.advance(2 * DAY)  # остался 1 день
    await h.lifecycle.tick()
    assert messages(h)[-1].startswith("Доступ закончится завтра")

    h.clock.advance(4 * DAY)  # день 13: период закончился на 10-й, плюс 3 дня льготы
    await h.lifecycle.tick()
    assert (await fetch(h, client.id)).state is State.SUSPENDED
    assert await open_jobs(h, client.id) == [(Op.SUSPEND, JobStatus.PENDING)]


async def test_paid_notice_has_the_tariffs_button(h: Harness) -> None:
    client = await make_client(h, State.ACTIVE, expires_at=NOW + DAY, renewal_on=0)
    renderer = TelegramNoticeRenderer("Europe/Moscow")
    _, markup = renderer.render(Notice(NoticeKind.WARN_1D, "1", False, at=NOW + DAY), client)
    assert markup is not None
    assert markup.inline_keyboard[0][0].callback_data == keyboards.MENU_TARIFFS


async def test_renewal_after_a_warning_cancels_the_countdown(h: Harness) -> None:
    from plugbackup_bot.services.billing import PaymentEvent

    end = NOW + 2 * DAY
    client = await make_client(
        h, State.ACTIVE, expires_at=end, renewal_on=0, sub_payload="v1:gb50:0:abcdef01",
        sub_charge_id="c1",
    )  # fmt: skip
    await h.lifecycle.tick()
    assert (await fetch(h, client.id)).state is State.EXPIRING

    renewal = PaymentEvent(
        tg_id=USER.id, currency="XTR", total_amount=50, payload="v1:gb50:0:abcdef01",
        charge_id="c2", provider_charge_id=None, is_recurring=True, is_first=False,
        sub_expires_at=end + 30 * DAY,
    )  # fmt: skip
    await h.billing.handle_payment(renewal)
    assert (await fetch(h, client.id)).state is State.ACTIVE

    sent = len(h.messenger.sent)
    h.clock.advance(10 * DAY)
    await h.lifecycle.tick()
    assert (await fetch(h, client.id)).state is State.ACTIVE
    assert len(h.messenger.sent) == sent


async def test_late_renewal_is_suspended_only_after_the_grace(h: Harness) -> None:
    end = NOW + DAY
    client = await make_client(h, State.ACTIVE, expires_at=end, renewal_on=1)
    h.clock.advance(DAY + 60)
    await h.lifecycle.tick()
    assert (await fetch(h, client.id)).state is State.EXPIRING
    assert messages(h)[-1].startswith("Продление ещё не прошло")

    h.clock.advance(2 * DAY)
    await h.lifecycle.tick()
    assert (await fetch(h, client.id)).state is State.EXPIRING  # ещё льгота, письмо за сутки
    h.clock.advance(DAY)
    await h.lifecycle.tick()
    assert (await fetch(h, client.id)).state is State.SUSPENDED


async def test_deleting_a_paid_client_stops_the_subscription(h: Harness) -> None:
    client = await make_client(
        h, State.SUSPENDED, sub_charge_id="charge-9", renewal_on=1, state_since=NOW - 20 * DAY
    )
    await h.lifecycle.tick()
    assert (await fetch(h, client.id)).state is State.DELETED
    assert h.stars.calls == [(USER.id, "charge-9", True)]


async def test_deleting_does_not_touch_an_already_stopped_subscription(h: Harness) -> None:
    await make_client(
        h, State.SUSPENDED, sub_charge_id="charge-9", renewal_on=0, state_since=NOW - 20 * DAY
    )
    await h.lifecycle.tick()
    assert h.stars.calls == []


async def test_manual_hold_survives_forever(h: Harness) -> None:
    client = await make_client(h, State.SUSPENDED, admin_hold=1)
    h.clock.advance(400 * DAY)
    await h.lifecycle.tick()
    assert (await fetch(h, client.id)).state is State.SUSPENDED
    assert h.messenger.sent == []


# --- ключ не прислан ---


async def test_pending_clients_are_reminded_once_per_stage(h: Harness) -> None:
    client = await make_client(h, State.PENDING, key=None)
    h.clock.advance(DAY)
    await h.lifecycle.tick()
    await h.lifecycle.tick()
    assert len(h.messenger.sent) == 1
    assert "ждёт ваш ключ" in messages(h)[0]

    h.clock.advance(2 * DAY)
    await h.lifecycle.tick()
    assert len(h.messenger.sent) == 2
    assert (await fetch(h, client.id)).state is State.PENDING


async def test_unclaimed_beta_expires_and_can_be_requested_again(h: Harness) -> None:
    client = await make_client(h, State.PENDING, plan="beta", key=None, quota_gb=10)
    async with h.db.transaction() as conn:
        await conn.execute("UPDATE users SET beta_used_at = ? WHERE tg_id = ?", (NOW, USER.id))

    h.clock.advance(7 * DAY)
    report = await h.lifecycle.tick()
    assert report.transitions == {"deleted": 1}
    assert (await fetch(h, client.id)).state is State.DELETED
    assert "код беты сгорел" in messages(h)[-1]
    assert await open_jobs(h, client.id) == []  # на ноде клиента не было
    async with h.db.read() as conn, conn.execute("SELECT beta_used_at FROM users") as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] is None


# --- устойчивость ---


async def test_stale_step_is_dropped(h: Harness) -> None:
    client = await make_client(h, State.ACTIVE, plan="beta", confirm_due_at=NOW, quota_gb=10)
    # Решение принято по снимку «active», но клиент уже заблокирован админом.
    async with h.db.transaction() as conn:
        await transition(conn, client.id, State.SUSPENDED, reason="t", actor="admin:1", now=NOW)
    step = Step(State.EXPIRING, "confirm_due", notices=(Notice(NoticeKind.BETA_PROMPT, "1", True),))
    await h.lifecycle._apply(client, step, NOW, TickReport())
    assert (await fetch(h, client.id)).state is State.SUSPENDED
    assert h.messenger.sent == []


async def test_one_broken_client_does_not_stop_the_others(
    h: Harness, db: Database, clock: FakeClock
) -> None:
    class Flaky:
        def __init__(self) -> None:
            self.inner = TelegramNoticeRenderer("Europe/Moscow")

        def render(self, notice: Notice, client: Client):  # type: ignore[no-untyped-def]
            if client.tg_id == 1:
                raise RuntimeError("boom")
            return self.inner.render(notice, client)

    flaky = LifecycleService(db, clock, h.access._catalog, h.billing, h.messenger, Flaky())
    bad = await make_client(h, State.ACTIVE, plan="beta", tg_id=1, confirm_due_at=NOW, quota_gb=10)
    good = await make_client(h, State.ACTIVE, plan="beta", tg_id=2, confirm_due_at=NOW, quota_gb=10)

    report = await flaky.tick()
    assert report.errors == 1
    assert (await fetch(h, good.id)).state is State.EXPIRING
    assert [tg_id for tg_id, _ in h.messenger.sent] == [2]
    assert (await fetch(h, bad.id)).state is State.EXPIRING  # переход записан, письмо потеряно


async def test_catching_up_after_downtime_keeps_a_full_grace_window(h: Harness) -> None:
    client = await make_client(h, State.ACTIVE, plan="beta", confirm_due_at=NOW, quota_gb=10)
    h.clock.advance(60 * DAY)  # бот не работал два месяца
    await h.lifecycle.tick()
    assert (await fetch(h, client.id)).state is State.EXPIRING
    await h.lifecycle.tick()
    assert (await fetch(h, client.id)).state is State.EXPIRING  # блокировка не сразу
    h.clock.advance(7 * DAY)
    await h.lifecycle.tick()
    assert (await fetch(h, client.id)).state is State.SUSPENDED


async def test_only_due_clients_are_touched(h: Harness) -> None:
    a = await make_client(h, State.ACTIVE, plan="beta", tg_id=1, confirm_due_at=NOW, quota_gb=10)
    b = await make_client(h, State.ACTIVE, plan="beta", tg_id=2, confirm_due_at=NOW + 99 * DAY)
    await h.lifecycle.tick()
    assert (await fetch(h, a.id)).state is State.EXPIRING
    assert (await fetch(h, b.id)).state is State.ACTIVE
    assert set(await _set_keys(h)) == {a.id}


async def _set_keys(h: Harness) -> list[int]:
    async with (
        h.db.read() as conn,
        conn.execute("SELECT DISTINCT client_id FROM notifications") as cur,
    ):
        return [row[0] for row in await cur.fetchall()]
