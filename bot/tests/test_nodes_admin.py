"""Ноды: добавление из бота, ёмкость, включение после проверки связи, режим обслуживания."""

import pytest
import pytest_asyncio
from aiogram.methods import AnswerCallbackQuery

from fakes import FakeClock
from harness import ADMIN, SUPPORT, USER, Harness
from helpers import NOW, add_client, add_node, add_user, plain
from plugbackup_bot import texts_ru as T
from plugbackup_bot.config import NodeConfig
from plugbackup_bot.db import Database
from plugbackup_bot.db import audit as audit_repo
from plugbackup_bot.db.capacity import pick_node
from plugbackup_bot.db.nodes import get_node, list_nodes, sync_nodes
from plugbackup_bot.domain.roles import Actor, Role
from plugbackup_bot.services.nodes import NodeResult
from plugbackup_bot.tg.admin import core
from plugbackup_bot.tg.admin.core import cb

OWNER = Actor(ADMIN.id, Role.OWNER)
N = core.NODES


@pytest_asyncio.fixture
async def h(db: Database, clock: FakeClock) -> Harness:
    harness = await Harness.build(db, clock)
    await harness.directory.add_support(OWNER, SUPPORT.id)
    async with db.transaction() as conn:
        await conn.execute("DELETE FROM audit_log")
    return harness


def config(**over: object) -> NodeConfig:
    base = {
        "name": "node2",
        "addr": "192.0.2.20",
        "public_host": "node2.example.org",
        "capacity_gb": 500,
    }
    return NodeConfig(**{**base, **over})  # type: ignore[arg-type]


async def node(h: Harness, node_id: int = 1):  # type: ignore[no-untyped-def]
    async with h.db.read() as conn:
        found = await get_node(conn, node_id)
    assert found is not None
    return found


async def actions(h: Harness) -> list[str]:
    async with h.db.read() as conn:
        entries = await audit_repo.page(conn, limit=50, offset=0)
    return [e.action for e in reversed(entries)]


# --- сервис ---


async def test_a_node_added_from_the_bot_starts_disabled(h: Harness) -> None:
    result, node_id = await h.nodes.add(OWNER, config())
    assert result is NodeResult.OK and node_id is not None
    added = await node(h, node_id)
    assert (added.enabled, added.source, added.maintenance) == (False, "bot", False)
    async with h.db.read() as conn:
        assert await pick_node(conn, 10) is not None and (await pick_node(conn, 10)).name == "node1"  # type: ignore[union-attr]
    assert await actions(h) == ["node.add"]


async def test_the_same_name_cannot_be_added_twice(h: Harness) -> None:
    assert (await h.nodes.add(OWNER, config(name="node1")))[0] is NodeResult.EXISTS
    assert len(await _all_nodes(h)) == 1


async def _all_nodes(h: Harness):  # type: ignore[no-untyped-def]
    async with h.db.read() as conn:
        return await list_nodes(conn)


async def test_enabling_needs_a_node_that_answers(h: Harness) -> None:
    _, node_id = await h.nodes.add(OWNER, config())
    h.set_node_up(False)
    assert await h.nodes.set_enabled(node_id, OWNER, True) is NodeResult.UNREACHABLE  # type: ignore[arg-type]
    assert not (await node(h, node_id)).enabled  # type: ignore[arg-type]
    h.set_node_up(True)
    assert await h.nodes.set_enabled(node_id, OWNER, True) is NodeResult.OK  # type: ignore[arg-type]
    assert (await node(h, node_id)).enabled  # type: ignore[arg-type]
    h.set_node_up(False)  # отключить можно всегда, даже если нода молчит
    assert await h.nodes.set_enabled(node_id, OWNER, False) is NodeResult.OK  # type: ignore[arg-type]
    assert await h.nodes.set_enabled(99, OWNER, True) is NodeResult.NOT_FOUND


async def test_capacity_and_oversell_limits_and_audit(h: Harness) -> None:
    assert await h.nodes.set_capacity(1, OWNER, 2000) is NodeResult.OK
    assert (await node(h)).capacity_gb == 2000 and (await node(h)).limit_gb == 2000
    assert await h.nodes.set_oversell(1, OWNER, 1.5) is NodeResult.OK
    assert (await node(h)).limit_gb == 3000
    for bad in (-1, 10_000_001):
        assert await h.nodes.set_capacity(1, OWNER, bad) is NodeResult.INVALID
    for bad in (0, -1, 10.5):
        assert await h.nodes.set_oversell(1, OWNER, bad) is NodeResult.INVALID
    assert await h.nodes.set_capacity(99, OWNER, 5) is NodeResult.NOT_FOUND
    assert await actions(h) == ["node.capacity", "node.oversell"]


async def test_maintenance_blocks_new_clients_notifies_and_ends(h: Harness) -> None:
    await add_user(h.db, 5)
    await add_client(h.db, 5, 1)
    assert await h.nodes.start_maintenance(1, OWNER, "работы") is NodeResult.OK
    assert (await node(h)).maintenance
    async with h.db.read() as conn:
        assert await pick_node(conn, 10) is None  # новых клиентов на ноду нет
        async with conn.execute("SELECT kind, node_id, text, ends_at FROM service_events") as cur:
            [event] = await cur.fetchall()
    assert tuple(event) == ("maintenance", 1, "работы", None)  # движок алертов разошлёт уведомление
    assert await h.nodes.start_maintenance(1, OWNER, "ещё раз") is NodeResult.WRONG_STATE

    h.clock.advance(3600)
    assert await h.nodes.end_maintenance(1, OWNER) is NodeResult.OK
    assert not (await node(h)).maintenance
    async with h.db.read() as conn:
        assert await pick_node(conn, 10) is not None
        async with conn.execute("SELECT ends_at FROM service_events") as cur:
            assert (await cur.fetchone())[0] == NOW + 3600  # type: ignore[index]
    assert await h.nodes.end_maintenance(1, OWNER) is NodeResult.WRONG_STATE
    assert await actions(h) == ["maintenance.start", "maintenance.end"]


async def test_a_stuck_maintenance_flag_can_be_cleared(h: Harness) -> None:
    async with h.db.transaction() as conn:
        await conn.execute("UPDATE nodes SET maintenance = 1 WHERE id = 1")
    assert await h.nodes.end_maintenance(1, OWNER) is NodeResult.OK
    assert not (await node(h)).maintenance


async def test_the_old_maint_command_uses_the_same_flag(h: Harness) -> None:
    await h.say("/maint node1 перезагрузка", user=ADMIN)
    assert (await node(h)).maintenance
    await h.say("/maint_end 1", user=ADMIN)
    assert not (await node(h)).maintenance
    await h.say("/maint all техработы", user=ADMIN)  # на все ноды: флаги нод не трогаются
    assert not (await node(h)).maintenance


async def test_restart_sync_keeps_nodes_added_from_the_bot(h: Harness) -> None:
    _, node_id = await h.nodes.add(OWNER, config())
    async with h.db.transaction() as conn:
        await conn.execute("UPDATE nodes SET enabled = 1 WHERE id = ?", (node_id,))
    # .env знает только node1: node2 из бота остаётся как есть, а вот пропавшая из .env отключится
    async with h.db.transaction() as conn:
        await sync_nodes(
            conn, [config(name="node1", addr="192.0.2.10", public_host="n1.example.org")]
        )
    assert (await node(h, node_id)).enabled  # type: ignore[arg-type]
    async with h.db.transaction() as conn:
        await sync_nodes(
            conn, [config(name="other", addr="192.0.2.30", public_host="o.example.org")]
        )
    assert not (await node(h, 1)).enabled  # нода из .env, которой больше нет в файле
    assert (await node(h, node_id)).enabled  # type: ignore[arg-type]


async def test_details_count_sold_space_and_live_clients(h: Harness) -> None:
    for tg_id, state in ((5, "active"), (6, "suspended"), (7, "deleted")):
        await add_user(h.db, tg_id)
        await add_client(h.db, tg_id, 1, state=state)
    details = await h.nodes.details(1)
    assert details is not None
    assert (details.sold_gb, details.clients, details.free_gb) == (100, 2, 900)
    assert await h.nodes.details(99) is None
    assert await h.nodes.check(1) is True and await h.nodes.check(99) is None


# --- экраны админа ---


def labels(h: Harness) -> list[str]:
    markup = h.last().reply_markup
    assert markup is not None
    return [b.text.replace(" ", " ") for row in markup.inline_keyboard for b in row]


async def test_the_node_list_and_card(h: Harness) -> None:
    await add_user(h.db, 5)
    await add_client(h.db, 5, 1)
    await h.say("/nodes", user=ADMIN)
    assert "node1 · 50/1000 ГБ · онлайн" in plain(h.last().text)
    await h.press(cb(N, "card", i=1), user=ADMIN)
    text = plain(h.last().text)
    expected = (
        "Нода node1",
        "Адрес: 192.0.2.10",
        "Ёмкость: 1000 ГБ",
        "Продано: 50 ГБ",
        "Свободно: 950 ГБ",
        "Клиентов: 1",
        "Статус: онлайн",
        "Источник: .env",
    )
    for line in expected:
        assert line in text, line
    assert "Ёмкость и адрес ноды из .env" in text


async def test_an_offline_node_shows_since_when(h: Harness) -> None:
    async with h.db.transaction() as conn:
        await conn.execute("UPDATE nodes SET down_since = ? WHERE id = 1", (NOW - 600,))
    await h.press(cb(N, "card", i=1), user=ADMIN)
    assert "офлайн, офлайн с" in plain(h.last().text)


async def test_editing_capacity_and_oversell(h: Harness) -> None:
    await h.press(cb(N, "cp", i=1), user=ADMIN)
    assert "Сейчас 1000" in plain(h.last().text)
    await h.say("-5", user=ADMIN)
    assert "Нужно целое число ГБ" in plain(h.last().text)
    await h.say("2000", user=ADMIN)
    assert "Ёмкость ноды node1: 2000 ГБ" in plain(h.last().text)
    await h.press(cb(N, "ov", i=1), user=ADMIN)
    await h.say("двадцать", user=ADMIN)
    assert "число больше 0" in plain(h.last().text)
    await h.say("1,5", user=ADMIN)  # запятая как десятичный разделитель
    assert "Коэффициент ноды node1: 1.5" in plain(h.last().text)
    assert (await node(h)).limit_gb == 3000


async def test_maintenance_goes_through_a_red_confirmation_and_back(h: Harness) -> None:
    await add_user(h.db, 5)
    await add_client(h.db, 5, 1)
    await h.press(cb(N, "mt", i=1), user=ADMIN)
    assert "1 её клиентам уйдёт уведомление" in plain(h.last().text)
    confirm = [
        b for row in h.last().reply_markup.inline_keyboard for b in row if b.style == "danger"
    ]
    assert len(confirm) == 1 and not (await node(h)).maintenance
    await h.press(confirm[0].callback_data, user=ADMIN)
    assert "Обслуживание ноды node1 включено" in plain(h.last().text)
    assert (await node(h)).maintenance
    assert any(name.endswith(T.BTN_NODE_MAINT_OFF) for name in labels(h))

    await h.press(cb(N, "me", i=1), user=ADMIN)
    assert "Её клиентам уйдёт сообщение о завершении" in plain(h.last().text)
    await h.press(cb(N, "me!", i=1), user=ADMIN)
    assert "завершено" in plain(h.last().text) and not (await node(h)).maintenance
    await h.press(cb(N, "me!", i=1), user=ADMIN)  # повторное нажатие
    assert T.NODE_STATE_CHANGED in plain(h.last().text)


async def test_check_enable_and_disable_buttons(h: Harness) -> None:
    await h.press(cb(N, "ck", i=1), user=ADMIN)
    assert "отвечает на SSH-порту" in plain(h.last().text)
    h.set_node_up(False)
    await h.press(cb(N, "ck", i=1), user=ADMIN)
    assert "не отвечает на SSH-порту" in plain(h.last().text)
    await h.press(cb(N, "en", i=1, p=0), user=ADMIN)
    assert "Нода node1 отключена" in plain(h.last().text) and not (await node(h)).enabled
    await h.press(cb(N, "en", i=1, p=1), user=ADMIN)  # нода молчит
    assert T.NODE_UNREACHABLE in plain(h.last().text) and not (await node(h)).enabled
    h.set_node_up(True)
    await h.press(cb(N, "en", i=1, p=1), user=ADMIN)
    assert "Нода node1 включена" in plain(h.last().text) and (await node(h)).enabled


async def test_the_add_wizard(h: Harness) -> None:
    await h.press(cb(N, "add"), user=ADMIN)
    assert "Имя ноды" in plain(h.last().text)
    await h.say("Node 2!", user=ADMIN)  # имя не подошло
    assert "Не подошло" in plain(h.last().text) and "Имя ноды" not in plain(h.last().text)[:5]
    await h.say("node2", user=ADMIN)
    assert "Адрес ноды для бота" in plain(h.last().text)
    await h.say("not an address", user=ADMIN)
    assert "Не подошло" in plain(h.last().text)
    await h.say("192.0.2.20", user=ADMIN)
    assert "Адрес для клиентов" in plain(h.last().text)
    await h.say("-bad-host", user=ADMIN)
    assert "Не подошло" in plain(h.last().text)
    await h.say("node2.example.org", user=ADMIN)
    assert "Ёмкость диска" in plain(h.last().text)
    await h.say("много", user=ADMIN)
    assert "нужно целое число" in plain(h.last().text)
    await h.say("500", user=ADMIN)

    text = plain(h.last().text)
    assert "Нода node2 добавлена и пока отключена" in text and "known_hosts" in text
    assert "Статус: отключена" in text and "Источник: добавлена из бота" in text
    added = (await _all_nodes(h))[1]
    assert (added.name, added.addr, added.public_host, added.capacity_gb, added.enabled) == (
        "node2", "192.0.2.20", "node2.example.org", 500, False,
    )  # fmt: skip
    assert await h.fsm_state(ADMIN) is None


async def test_the_wizard_refuses_an_existing_name_and_can_be_cancelled(h: Harness) -> None:
    await h.press(cb(N, "add"), user=ADMIN)
    await h.say("node1", user=ADMIN)
    await h.say("192.0.2.20", user=ADMIN)
    await h.say("node1.example.org", user=ADMIN)
    await h.say("100", user=ADMIN)
    assert T.NODE_ADD_EXISTS in plain(h.last().text)
    assert len(await _all_nodes(h)) == 1
    await h.press(cb(N, "add"), user=ADMIN)
    await h.say("/cancel", user=ADMIN)
    assert await h.fsm_state(ADMIN) is None


@pytest.mark.parametrize(
    "data",
    [cb(N, "list"), cb(N, "card", i=1), cb(N, "cp", i=1), cb(N, "ov", i=1), cb(N, "ck", i=1),
     cb(N, "en", i=1, p=0), cb(N, "mt", i=1), cb(N, "mt!", i=1), cb(N, "me!", i=1), cb(N, "add")],
)  # fmt: skip
async def test_support_cannot_manage_nodes(h: Harness, data: str) -> None:
    await h.press(data, user=SUPPORT)
    answer = h.session.of(AnswerCallbackQuery)[-1]
    assert (answer.text, answer.show_alert) == (T.ADM_DENIED, True)
    before = await node(h)
    assert before.enabled and not before.maintenance and before.capacity_gb == 1000
    assert not await actions(h)
    await h.say("/nodes", user=SUPPORT)
    assert h.last().text == T.ADM_DENIED


def test_unused_imports_for_the_harness() -> None:
    assert USER and add_node  # стенд создаёт ноду сам
