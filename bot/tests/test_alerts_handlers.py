"""Экран /alerts через настоящий Dispatcher."""

import pytest
import pytest_asyncio
from aiogram.methods import EditMessageReplyMarkup, EditMessageText

from fakes import FakeClock
from harness import USER, Harness
from helpers import NOW, add_client, add_user, label, plain
from plugbackup_bot import texts_ru as T
from plugbackup_bot.db import Database
from plugbackup_bot.db import alerts as alerts_repo
from plugbackup_bot.tg import alert_ui as ui


@pytest_asyncio.fixture
async def h(db: Database, clock: FakeClock) -> Harness:
    harness = await Harness.build(db, clock)
    await add_user(db, USER.id)
    await add_client(db, USER.id, 1)
    return harness


def edit(h: Harness) -> EditMessageText:
    return h.session.of(EditMessageText)[-1]


def edit_buttons(h: Harness) -> list[tuple[str, str | None]]:
    markup = edit(h).reply_markup
    assert markup is not None
    return [(label(b.text), b.callback_data) for row in markup.inline_keyboard for b in row]


async def settings(h: Harness):  # type: ignore[no-untyped-def]
    async with h.db.read() as conn:
        return await alerts_repo.get_settings(conn, USER.id)


async def prefs(h: Harness):  # type: ignore[no-untyped-def]
    async with h.db.read() as conn:
        return await alerts_repo.get_prefs(conn, USER.id, "Europe/Moscow")


async def test_needs_a_storage(db: Database, clock: FakeClock) -> None:
    empty = await Harness.build(db, clock)
    await empty.say("/alerts")
    assert empty.texts() == [T.NO_STORAGE]


async def test_main_screen(h: Harness) -> None:
    await h.say("/alerts")
    text = plain(h.last().text)
    assert text.splitlines()[0].endswith("Уведомления")
    assert (
        "Всегда включены, отключить их нельзя: окончание подписки, блокировка хранилища, "
        "удаление хранилища, смена SSH-ключа." in text
    )
    assert "Тихие часы: 23:00–08:00, пояс: Москва (UTC+3)" in text
    assert "не чаще раза в 24 ч" in text
    assert "• Нет бэкапов — вкл, 3 дн." in text
    assert "• Заполнено на 80% — выкл" in text
    assert "приходит и в тихие часы" in text

    buttons = h.last_buttons()
    toggles = [(label, data) for label, data, _ in buttons if (data or "").startswith("al:t:")]
    assert len(toggles) == 10
    assert toggles[0] == ("Вкл · Нет бэкапов", "al:t:stale")
    assert ("Выкл · Заполнено на 80%", "al:t:fill_80") in toggles
    assert [data for _, data, _ in buttons if (data or "").startswith("al:th:")] == [
        "al:th:stale",
        "al:th:drop",
        "al:th:spike",
        "al:th:fails",
    ]
    assert [data for _, data, _ in buttons[-3:]] == ["al:q", "al:tz", "al:cd"]


async def test_toggle_updates_the_screen_and_the_database(h: Harness) -> None:
    await h.say("/alerts")
    await h.press("al:t:fill_80")
    assert (await settings(h))["fill_80"].enabled is True
    assert "• Заполнено на 80% — вкл" in edit(h).text
    assert ("Вкл · Заполнено на 80%", "al:t:fill_80") in edit_buttons(h)

    await h.press("al:t:fill_80")
    assert (await settings(h))["fill_80"].enabled is False
    assert "• Заполнено на 80% — выкл" in edit(h).text


async def test_default_state_is_not_stored_until_changed(h: Harness) -> None:
    current = await settings(h)
    assert current["fill_90"].enabled and current["fill_100"].enabled
    assert not current["fill_80"].enabled
    async with h.db.read() as conn, conn.execute("SELECT COUNT(*) FROM alert_settings") as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == 0


async def test_threshold_selection(h: Harness) -> None:
    await h.press("al:th:stale")
    assert "выберите порог" in edit(h).text
    assert [name.replace("\u00a0", " ") for name, _ in edit_buttons(h)] == [
        "1 дн.",
        "3 дн.",
        "7 дн.",
        "Назад",
        "Главное меню",
    ]

    await h.press("al:sv:stale:7")
    assert (await settings(h))["stale"].threshold == 7
    assert "• Нет бэкапов — вкл, 7 дн." in plain(edit(h).text)


@pytest.mark.parametrize(
    "data", ["al:sv:stale:99", "al:sv:stale:abc", "al:sv:fill_80:1", "al:sv:nope:1"]
)
async def test_invalid_thresholds_are_ignored(h: Harness, data: str) -> None:
    before = await settings(h)
    await h.press(data)
    assert await settings(h) == before


async def test_alerts_without_thresholds_have_no_threshold_screen(h: Harness) -> None:
    await h.press("al:th:fill_80")
    assert h.session.of(EditMessageText) == []


async def test_new_ip_needs_a_consent_screen(h: Harness) -> None:
    await h.press("al:t:new_ip")
    text = plain(edit(h).text)
    assert "HMAC-хеш" in text
    assert "сырые IP ноду не покидают" in text
    assert "не больше 20 последних хешей" in text
    assert "не дольше 30 дней" in text
    assert "хеши удаляются сразу" in text
    assert "PlugBackup не скрывает ваш адрес от ноды" in text
    assert (await settings(h))["new_ip"].enabled is False
    assert ("Включить алерт", ui.CONSENT_YES) in edit_buttons(h)

    await h.press(ui.CONSENT_YES)
    setting = (await settings(h))["new_ip"]
    assert setting.enabled is True
    assert setting.consent_at == NOW


async def test_turning_new_ip_off_erases_the_hashes(h: Harness) -> None:
    await h.press("al:t:new_ip")
    await h.press(ui.CONSENT_YES)
    async with h.db.transaction() as conn:
        await alerts_repo.record_ip_hashes(conn, 1, {"a" * 16: NOW}, NOW, baseline=True)
    await h.press("al:t:new_ip")  # теперь это выключение, согласие не спрашиваем
    assert (await settings(h))["new_ip"].enabled is False
    async with h.db.read() as conn:
        assert await alerts_repo.known_hash_count(conn, 1) == 0
    assert (await settings(h))["new_ip"].consent_at is None


async def test_quiet_hours_timezone_and_cooldown(h: Harness) -> None:
    await h.press(ui.QUIET)
    assert [label for label, _ in edit_buttons(h)] == [
        "23:00–08:00",
        "22:00–07:00",
        "00:00–06:00",
        "Выключены",
        "Назад",
        "Главное меню",
    ]
    await h.press(f"{ui.SET_QUIET}1")
    assert (await prefs(h)).quiet_from == 22
    assert "Тихие часы: 22:00–07:00" in plain(edit(h).text)

    await h.press(f"{ui.SET_QUIET}3")
    assert "Тихие часы: выключены" in plain(edit(h).text)

    await h.press(ui.TZ)
    assert len(edit_buttons(h)) == 8  # шесть поясов, «Назад» и «Главное меню»
    await h.press(f"{ui.SET_TZ}2")
    assert (await prefs(h)).tz == "Asia/Yekaterinburg"
    assert "пояс: Екатеринбург (UTC+5)" in plain(edit(h).text)

    await h.press(ui.COOLDOWN)
    await h.press(f"{ui.SET_COOLDOWN}12")
    assert (await prefs(h)).cooldown_hours == 12
    assert "не чаще раза в 12 ч" in plain(edit(h).text)


@pytest.mark.parametrize(
    "data", [f"{ui.SET_QUIET}99", f"{ui.SET_TZ}99", f"{ui.SET_COOLDOWN}5", f"{ui.SET_QUIET}x"]
)
async def test_invalid_preferences_are_ignored(h: Harness, data: str) -> None:
    before = await prefs(h)
    await h.press(data)
    assert await prefs(h) == before


async def test_preferences_survive_a_toggle(h: Harness) -> None:
    await h.press(f"{ui.SET_COOLDOWN}6")
    await h.press(f"{ui.SET_TZ}0")
    await h.press("al:t:fill_80")
    assert (await prefs(h)).cooldown_hours == 6
    assert (await prefs(h)).tz == "Europe/Kaliningrad"


async def test_disable_button_under_an_alert_message(h: Harness) -> None:
    await h.press("al:off:drop")
    assert (await settings(h))["drop"].enabled is False
    assert h.callback_answers()[-1].text == T.ALERT_OFF_DONE
    assert len(h.session.of(EditMessageReplyMarkup)) == 1


async def test_disable_button_with_an_unknown_key_changes_nothing(h: Harness) -> None:
    before = await settings(h)
    await h.press("al:off:nope")
    assert await settings(h) == before
    assert h.session.of(EditMessageReplyMarkup) == []


async def test_back_returns_to_the_main_screen(h: Harness) -> None:
    await h.press(ui.QUIET)
    await h.press(ui.BACK)
    assert plain(edit(h).text).splitlines()[0].endswith("Уведомления")
