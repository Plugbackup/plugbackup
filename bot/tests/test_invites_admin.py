"""Инвайты: лимит использований, срок, отзыв, список и карточка в админке."""

import pytest
import pytest_asyncio
from aiogram.methods import AnswerCallbackQuery

from fakes import FakeClock
from harness import ADMIN, SUPPORT, USER, Harness
from helpers import NOW, add_user, plain
from plugbackup_bot import texts_ru as T
from plugbackup_bot.db import Database
from plugbackup_bot.db import audit as audit_repo
from plugbackup_bot.db import invites as invites_repo
from plugbackup_bot.db.risk import ack_risk
from plugbackup_bot.domain.roles import Actor, Role
from plugbackup_bot.services.access import RedeemKind
from plugbackup_bot.services.invites import InviteStatus
from plugbackup_bot.tg import texts
from plugbackup_bot.tg.admin import core
from plugbackup_bot.tg.admin.core import cb

OWNER = Actor(ADMIN.id, Role.OWNER)
V = core.INVITES
DAY = 24 * 3600


@pytest_asyncio.fixture
async def h(db: Database, clock: FakeClock) -> Harness:
    harness = await Harness.build(db, clock)
    await harness.directory.add_support(OWNER, SUPPORT.id)
    async with db.transaction() as conn:
        await conn.execute("DELETE FROM audit_log")
    return harness


async def ready(h: Harness, tg_id: int) -> None:
    """Пользователь, который согласился с рисками беты и может ввести код."""
    await add_user(h.db, tg_id)
    async with h.db.transaction() as conn:
        await ack_risk(conn, tg_id, texts.RISK_TEXT_VERSION, NOW)


async def redeem(h: Harness, tg_id: int, code: str) -> RedeemKind:
    await ready(h, tg_id)
    return (await h.access.redeem_invite(tg_id, code)).kind


# --- погашение ---


async def test_a_multi_use_code_works_until_the_limit(h: Harness) -> None:
    [code] = await h.invites.create(OWNER, 1, max_uses=2)
    assert await redeem(h, 10, code) is RedeemKind.OK
    assert await redeem(h, 11, code) is RedeemKind.OK
    assert await redeem(h, 12, code) is RedeemKind.USED  # лимит исчерпан
    rows, _, active = await h.invites.page(limit=10, offset=0)
    assert (rows[0].uses, rows[0].max_uses, active) == (2, 2, 0)
    details = await h.invites.details(rows[0].id)
    assert details is not None and [u.tg_id for u in details.uses] == [10, 11]


async def test_one_use_codes_stay_one_use_by_default(h: Harness) -> None:
    [code] = await h.invites.create(OWNER, 1)
    assert await redeem(h, 10, code) is RedeemKind.OK
    assert await redeem(h, 11, code) is RedeemKind.USED


async def test_an_expired_code_is_refused_with_its_own_message(h: Harness) -> None:
    [code] = await h.invites.create(OWNER, 1, expires_days=7)
    h.clock.advance(7 * DAY - 1)
    await ready(h, 10)
    assert (await h.access.redeem_invite(10, code)).kind is RedeemKind.OK
    [late] = await h.invites.create(OWNER, 1, expires_days=7)
    h.clock.advance(7 * DAY)
    assert await redeem(h, 11, late) is RedeemKind.EXPIRED
    assert texts.INVITE_ERRORS[RedeemKind.EXPIRED] == T.INVITE_EXPIRED


async def test_a_revoked_code_looks_like_a_wrong_code(h: Harness) -> None:
    [code] = await h.invites.create(OWNER, 1)
    rows, _, _ = await h.invites.page(limit=1, offset=0)
    assert await h.invites.revoke(rows[0].id, OWNER) is not None
    assert await redeem(h, 10, code) is RedeemKind.INVALID  # причину отзыва не раскрываем


async def test_the_database_never_stores_the_code_only_its_tail(h: Harness) -> None:
    [code] = await h.invites.create(OWNER, 1)
    rows, _, _ = await h.invites.page(limit=1, offset=0)
    tail = code.replace("-", "")[-3:]
    assert rows[0].code_tail == tail and rows[0].label == f"…{tail}"
    async with h.db.read() as conn, conn.execute("SELECT code_hash, code_tail FROM invites") as cur:
        [row] = await cur.fetchall()
    assert code.replace("-", "") not in str(tuple(row))


async def test_creation_limits(h: Harness) -> None:
    for kwargs in ({"count": 0}, {"count": 51}, {"count": 1, "max_uses": 0},
                   {"count": 1, "max_uses": 1001}, {"count": 1, "expires_days": 0},
                   {"count": 1, "expires_days": 366}):  # fmt: skip
        with pytest.raises(ValueError):
            await h.invites.create(OWNER, **kwargs)
    assert await _total(h) == 0


async def _total(h: Harness) -> int:
    _, total, _ = await h.invites.page(limit=1, offset=0)
    return total


async def test_unused_count_ignores_revoked_expired_and_used(h: Harness) -> None:
    codes = await h.invites.create(OWNER, 3)
    await h.invites.create(OWNER, 1, expires_days=1)
    rows, _, active = await h.invites.page(limit=10, offset=0)
    assert active == 4
    await h.invites.revoke(rows[3].id, OWNER)  # самый старый из трёх
    await redeem(h, 10, codes[1])
    h.clock.advance(2 * DAY)
    async with h.db.read() as conn:
        assert await invites_repo.count_unused(conn, h.clock.now()) == 1


async def test_statuses_in_the_list(h: Harness) -> None:
    [code] = await h.invites.create(OWNER, 1)
    await h.invites.create(OWNER, 1, expires_days=1)
    await redeem(h, 10, code)
    await h.invites.create(OWNER, 1)
    rows, _, _ = await h.invites.page(limit=10, offset=0)
    await h.invites.revoke(rows[0].id, OWNER)
    h.clock.advance(2 * DAY)
    statuses = [row.status(h.clock.now()) for row in (await h.invites.page(limit=10, offset=0))[0]]
    assert statuses == [
        InviteStatus.REVOKED,
        InviteStatus.EXPIRED,
        InviteStatus.USED,
    ]


async def test_revoking_is_logged_and_repeats_are_harmless(h: Harness) -> None:
    await h.invites.create(OWNER, 1)
    rows, _, _ = await h.invites.page(limit=1, offset=0)
    first = await h.invites.revoke(rows[0].id, OWNER)
    second = await h.invites.revoke(rows[0].id, OWNER)
    missing = await h.invites.revoke(999, OWNER)
    assert (first.value, second.value, missing.value) == ("ok", "already", "not_found")
    async with h.db.read() as conn:
        entries = await audit_repo.page(conn, limit=10, offset=0)
    assert [e.action for e in reversed(entries)] == ["invite.create", "invite.revoke"]


# --- экраны админа ---


async def test_the_wizard_creates_codes_with_the_chosen_limits(h: Harness) -> None:
    await h.press(cb(V, "n"), user=ADMIN)
    assert "Сколько кодов создать" in plain(h.last().text)
    counts = [name for name in _labels(h) if "шт." in name]
    assert counts == ["1 шт.", "5 шт.", "10 шт.", "20 шт."]

    await h.press(cb(V, "n2", i=5), user=ADMIN)
    assert "Создаю 5 шт." in plain(h.last().text)
    await h.press(cb(V, "n3", i=5, p=3), user=ADMIN)
    assert "использований у каждого: 3" in plain(h.last().text)
    await h.press(cb(V, "mk", i=5, p=3, x="d30"), user=ADMIN)

    shown = plain(h.last().text)
    assert "Коды готовы" in shown and "5 шт., использований у каждого: 3" in shown
    assert "до " in shown  # срок
    raw_codes = [part.split("</code>")[0] for part in h.last().text.split("<code>")[1:]]
    assert len(raw_codes) == 5
    copy = [
        b.copy_text.text
        for row in h.last().reply_markup.inline_keyboard
        for b in row
        if b.copy_text
    ]
    assert copy == ["\n".join(raw_codes)]  # все коды копируются одной кнопкой
    rows, total, _ = await h.invites.page(limit=10, offset=0)
    assert total == 5 and all(r.max_uses == 3 and r.expires_at == NOW + 30 * DAY for r in rows)
    assert await redeem(h, 20, raw_codes[0]) is RedeemKind.OK


async def test_a_typed_count_and_no_expiry(h: Harness) -> None:
    await h.press(cb(V, "n"), user=ADMIN)
    await h.say("много", user=ADMIN)
    assert "Нужно число кодов от 1 до 50" in plain(h.last().text)
    await h.say("2", user=ADMIN)
    assert "Создаю 2 шт." in plain(h.last().text)
    await h.press(cb(V, "n3", i=2, p=1), user=ADMIN)
    await h.press(cb(V, "mk", i=2, p=1, x="d0"), user=ADMIN)
    assert "без срока" in plain(h.last().text)
    rows, _, _ = await h.invites.page(limit=10, offset=0)
    assert all(r.expires_at is None for r in rows)


async def test_fifty_codes_do_not_fit_one_copy_button(h: Harness) -> None:
    await h.press(cb(V, "mk", i=50, p=1, x="d0"), user=ADMIN)
    markup = h.last().reply_markup
    assert not any(b.copy_text for row in markup.inline_keyboard for b in row)
    assert h.last().text.count("<code>") == 50  # а сами коды показаны


async def test_the_invite_command(h: Harness) -> None:
    await h.say("/invite 3", user=ADMIN)
    assert h.last().text.count("<code>") == 3
    await h.say("/invite 99", user=ADMIN)
    assert "от 1 до 50" in plain(h.last().text)
    await h.say("/invite", user=ADMIN)  # без числа: список
    assert "Инвайты" in plain(h.last().text) and "Всего: 3" in plain(h.last().text)


async def test_the_list_pages_and_labels(h: Harness) -> None:
    await h.invites.create(OWNER, 20)
    await h.press(cb(V, "list"), user=ADMIN)
    rows = [name for name in _labels(h) if "/1" in name]
    assert len(rows) == 8 and all(" · 0/1 · активен" in name for name in rows)
    assert "Всего: 20, действующих: 20" in plain(h.last().text)
    await h.press(cb(V, "list", p=2), user=ADMIN)
    assert len([name for name in _labels(h) if "/1" in name]) == 4
    assert "3 из 3" in " ".join(_labels(h))


async def test_the_card_shows_who_used_the_code(h: Harness) -> None:
    [code] = await h.invites.create(OWNER, 1, max_uses=3)
    await redeem(h, 10, code)
    async with h.db.transaction() as conn:
        await conn.execute("UPDATE users SET username = 'anna' WHERE tg_id = 10")
    rows, _, _ = await h.invites.page(limit=1, offset=0)
    await h.press(cb(V, "card", i=rows[0].id), user=ADMIN)
    text = plain(h.last().text)
    assert f"Инвайт …{code.replace('-', '')[-3:]}" in text
    assert "Использований: 1 из 3" in text
    assert "@anna" in text and "10" in text and "u100" in text
    await h.press(cb(V, "card", i=999), user=ADMIN)
    assert T.ADM_NOT_FOUND in plain(h.last().text)


async def test_revoking_needs_a_red_confirmation(h: Harness) -> None:
    await h.invites.create(OWNER, 1)
    rows, _, _ = await h.invites.page(limit=1, offset=0)
    await h.press(cb(V, "rv", i=rows[0].id), user=ADMIN)
    assert "Отозвать инвайт" in plain(h.last().text)
    confirm = [
        b for row in h.last().reply_markup.inline_keyboard for b in row if b.style == "danger"
    ]
    assert [b.text.endswith(T.BTN_ADM_CONFIRM) for b in confirm] == [True]
    assert (await h.invites.page(limit=1, offset=0))[0][0].revoked_at is None  # пока не отозван
    await h.press(confirm[0].callback_data, user=ADMIN)
    assert "отозван" in plain(h.last().text)
    await h.press(confirm[0].callback_data, user=ADMIN)  # повторное нажатие
    assert T.INV_REVOKE_ALREADY in plain(h.last().text)


@pytest.mark.parametrize(
    "data",
    [
        cb(V, "list"),
        cb(V, "n"),
        cb(V, "n2", i=5),
        cb(V, "n3", i=5, p=1),
        cb(V, "mk", i=5, p=1, x="d0"),
        cb(V, "card", i=1),
        cb(V, "rv", i=1),
        cb(V, "rv!", i=1),
    ],
)
async def test_support_cannot_manage_invites(h: Harness, data: str) -> None:
    await h.invites.create(OWNER, 1)
    await h.press(data, user=SUPPORT)
    answer = h.session.of(AnswerCallbackQuery)[-1]
    assert (answer.text, answer.show_alert) == (T.ADM_DENIED, True)
    assert await _total(h) == 1  # новых кодов не появилось, старый не отозван
    assert (await h.invites.page(limit=1, offset=0))[0][0].revoked_at is None
    await h.say("/invite 2", user=SUPPORT)
    assert h.last().text == T.ADM_DENIED


async def test_a_beta_client_sees_the_expiry_message(h: Harness) -> None:
    [code] = await h.invites.create(OWNER, 1, expires_days=1)
    h.clock.advance(3 * DAY)
    await h.press("risk:beta", user=USER)
    await h.say(code, user=USER)
    assert h.last().text == T.INVITE_EXPIRED


def _labels(h: Harness) -> list[str]:
    markup = h.last().reply_markup
    assert markup is not None
    return [b.text.replace(" ", " ") for row in markup.inline_keyboard for b in row]
