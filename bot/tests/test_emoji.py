"""Эмодзи: имена, кастомный и запасной вид, пак, сопоставление, /emoji_preview."""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import InlineKeyboardMarkup, Sticker, StickerSet

from fakes import FakeClock, check_telegram_html, make_bot
from harness import ADMIN, USER, Harness
from plugbackup_bot import emoji_tools
from plugbackup_bot.db import Database
from plugbackup_bot.emoji import CANDIDATES, GLYPHS, NAMES, E, EmojiRegistry, plain_key
from plugbackup_bot.emoji_tools import EmojiToolError, Pack, PackItem
from plugbackup_bot.runtime import _setup_emoji
from plugbackup_bot.tg.emoji_fallback import (
    EmojiFallback,
    plain_updates,
    probing,
    strip_custom_emoji,
)
from plugbackup_bot.tg.handlers import emoji_preview

BOT_DIR = Path(__file__).resolve().parents[1]
REQUIRED_NAMES = (
    "ok error warning lock shield key disk upload download payment star bell settings back home "
    "chart clock user admin trash refresh invite support"
).split()
IDS = {"ok": "5000000000000000001", "lock": "5000000000000000002", "star": "5000000000000000003"}


def configured(*, enabled: bool = True) -> EmojiRegistry:
    E.configure(IDS, enabled=enabled)
    return E


# --- имена и вид ---


def test_all_semantic_names_from_the_brief_exist() -> None:
    assert set(REQUIRED_NAMES) == set(NAMES)
    assert len(set(NAMES)) == len(NAMES)


def test_every_name_has_a_plain_glyph_and_candidates() -> None:
    for name in NAMES:
        assert GLYPHS[name] in CANDIDATES[name]
        assert len(CANDIDATES[name]) >= 1


def test_without_setup_the_interface_uses_plain_emoji() -> None:
    assert str(E.ok) == "✅"
    assert f"{E.lock} закрыто" == "🔒 закрыто"
    assert E.ok.custom_id is None


def test_custom_emoji_render_as_tg_emoji_tags() -> None:
    configured()
    assert str(E.ok) == '<tg-emoji emoji-id="5000000000000000001">✅</tg-emoji>'
    assert E.ok.custom_id == IDS["ok"]
    check_telegram_html(f"{E.ok} Готово")
    assert str(E.key) == "🔑"  # id для имени не задан: обычный эмодзи


def test_the_flag_off_means_plain_emoji_everywhere() -> None:
    configured(enabled=False)
    assert str(E.ok) == "✅" and E.ok.custom_id is None
    assert E.mapping()["ok"] == IDS["ok"]  # сопоставление при этом помнится для /emoji_preview


def test_degrading_switches_to_plain_and_retry_switches_back() -> None:
    configured()
    assert E.degrade("DOCUMENT_INVALID") is True
    assert E.degrade("DOCUMENT_INVALID") is False  # во второй раз «впервые» уже не бывает
    assert str(E.ok) == "✅" and not E.active
    E.retry()
    assert E.active and "tg-emoji" in str(E.ok)


def test_bad_ids_and_unknown_names_are_ignored() -> None:
    E.configure(
        {"ok": '1" onload="x', "lock": "12a", "star": "٣٣", "nope": "123", "key": None},
        enabled=True,
    )
    assert all(value is None for value in E.mapping().values())
    assert str(E.ok) == "✅"


def test_a_misspelled_name_is_a_loud_error() -> None:
    with pytest.raises(AttributeError):
        _ = E.okk
    with pytest.raises(KeyError):
        E.icon("okk")


def test_glyph_lookup_by_id() -> None:
    configured()
    assert E.glyph_for_id(IDS["lock"]) == "🔒"
    assert E.glyph_for_id("999") is None


# --- кнопки ---


def test_a_button_with_a_custom_icon_does_not_repeat_the_emoji_in_the_text() -> None:
    configured()
    button = E.button("Оплатить", icon=E.ok, callback_data="x", style="success")
    assert button.icon_custom_emoji_id == IDS["ok"]
    assert button.text == "Оплатить" and "✅" not in button.text
    assert button.style == "success"


def test_a_plain_button_gets_the_emoji_before_the_text() -> None:
    configured(enabled=False)
    button = E.button("Оплатить", icon=E.ok, callback_data="x")
    assert button.icon_custom_emoji_id is None
    assert button.text == "✅ Оплатить"
    assert E.button("Назад", callback_data="y").text == "Назад"


# --- сопоставление с паком ---


def pack_of(*pairs: tuple[str, str]) -> Pack:
    return Pack("Set", "Title", "2026-01-01T00:00:00Z", tuple(PackItem(e, i) for e, i in pairs))


def test_mapping_ignores_variation_selectors() -> None:
    assert plain_key("⚠️") == plain_key("⚠") == "⚠"
    mapping = emoji_tools.build_map(pack_of(("⚠", "11"), ("🔒", "22"), ("✔", "33")))
    assert mapping["warning"] == "11" and mapping["lock"] == "22" and mapping["ok"] == "33"
    assert mapping["shield"] is None


def test_mapping_prefers_the_first_candidate() -> None:
    mapping = emoji_tools.build_map(pack_of(("👍", "9"), ("✅", "1")))
    assert mapping["ok"] == "1"


def test_hand_edits_survive_a_rescan_unless_remap() -> None:
    pack = pack_of(("✅", "1"), ("🔒", "2"))
    edited = {"ok": "777", "lock": None}
    kept = emoji_tools.build_map(pack, edited)
    assert kept["ok"] == "777"  # правка вручную сохранилась
    assert kept["lock"] == "2"  # пустое место заполнилось
    assert emoji_tools.build_map(pack, edited, remap=True)["ok"] == "1"


def test_the_map_file_reader_is_forgiving(tmp_path: Path) -> None:
    path = tmp_path / "map.json"
    assert emoji_tools.read_map(path) == {}  # файла нет
    path.write_text("{not json", encoding="utf-8")
    assert emoji_tools.read_map(path) == {}
    path.write_text("[1]", encoding="utf-8")
    assert emoji_tools.read_map(path) == {}
    path.write_text(
        json.dumps({"_note": "x", "ok": "5", "lock": None, "mystery": "6"}), encoding="utf-8"
    )
    assert emoji_tools.read_map(path) == {"ok": "5", "lock": None}


def test_the_committed_pack_and_map_agree() -> None:
    pack = emoji_tools.read_pack(BOT_DIR / "emoji_pack.json")
    mapping = emoji_tools.read_map(BOT_DIR / "emoji_map.json")
    known = {item.custom_emoji_id for item in pack.items}
    assert pack.set_name == "RestrictedEmoji" and len(pack.items) > 100
    assert set(mapping) == set(NAMES)
    for name, custom_id in mapping.items():
        assert custom_id is None or custom_id in known, name
    assert sum(value is not None for value in mapping.values()) >= 15


# --- загрузка пака ---


def sticker(emoji: str | None, custom_id: str | None) -> Sticker:
    return Sticker(
        file_id="f",
        file_unique_id="u",
        type="custom_emoji",
        width=100,
        height=100,
        is_animated=True,
        is_video=False,
        emoji=emoji,
        custom_emoji_id=custom_id,
    )


def a_set(*stickers: Sticker) -> StickerSet:
    return StickerSet(
        name="RestrictedEmoji",
        title="Animated Emoji",
        sticker_type="custom_emoji",
        stickers=list(stickers),
    )


async def test_scanning_reads_custom_emoji_from_the_set() -> None:
    bot, session = make_bot()
    session.sticker_sets["RestrictedEmoji"] = a_set(
        sticker("✅", "1"), sticker("🔒", "2"), sticker("🙂", None)
    )
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    pack = await emoji_tools.scan_pack(bot, "RestrictedEmoji", now=now)
    assert [(i.emoji, i.custom_emoji_id) for i in pack.items] == [("✅", "1"), ("🔒", "2")]
    assert pack.scanned_at == "2026-11-05T12:00:00Z" and pack.title == "Animated Emoji"
    assert emoji_tools.pack_from_json(emoji_tools.pack_to_json(pack)) == pack


async def test_an_empty_set_is_an_error() -> None:
    bot, session = make_bot()
    session.sticker_sets["RestrictedEmoji"] = a_set(sticker("🙂", None))
    with pytest.raises(EmojiToolError):
        await emoji_tools.scan_pack(bot, "RestrictedEmoji")


def test_a_broken_pack_file_is_explained(tmp_path: Path) -> None:
    with pytest.raises(EmojiToolError, match="scan_emoji"):
        emoji_tools.read_pack(tmp_path / "none.json")
    bad = tmp_path / "bad.json"
    bad.write_text('{"set_name": "x"}', encoding="utf-8")
    with pytest.raises(EmojiToolError, match="повреждён"):
        emoji_tools.read_pack(bad)
    bad.write_text("nope", encoding="utf-8")
    with pytest.raises(EmojiToolError, match="JSON"):
        emoji_tools.read_pack(bad)


@pytest.fixture
def scan_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOT_TOKEN", "123456789:" + "x" * 35)
    monkeypatch.setenv("EMOJI_PACK_FILE", str(tmp_path / "pack.json"))
    monkeypatch.setenv("EMOJI_MAP_FILE", str(tmp_path / "map.json"))
    return tmp_path


async def test_the_scan_script_writes_both_files(scan_env: Path) -> None:
    bot, session = make_bot()
    session.sticker_sets["RestrictedEmoji"] = a_set(sticker("✅", "1"), sticker("🔒", "2"))
    lines: list[str] = []
    code = await emoji_tools.run([], bot_factory=lambda token: bot, out=lines.append)
    assert code == 0
    assert emoji_tools.read_pack(scan_env / "pack.json").items[0].custom_emoji_id == "1"
    mapping = emoji_tools.read_map(scan_env / "map.json")
    assert mapping["ok"] == "1" and mapping["lock"] == "2" and mapping["shield"] is None
    assert "Не нашлось в паке" in "\n".join(lines) and "/emoji_preview" in "\n".join(lines)


async def test_a_rescan_keeps_hand_edits(scan_env: Path) -> None:
    bot, session = make_bot()
    session.sticker_sets["RestrictedEmoji"] = a_set(sticker("✅", "1"), sticker("🔒", "2"))
    await emoji_tools.run([], bot_factory=lambda token: bot, out=lambda line: None)
    data = json.loads((scan_env / "map.json").read_text(encoding="utf-8"))
    data["ok"] = "424242"
    (scan_env / "map.json").write_text(json.dumps(data), encoding="utf-8")
    await emoji_tools.run([], bot_factory=lambda token: bot, out=lambda line: None)
    assert emoji_tools.read_map(scan_env / "map.json")["ok"] == "424242"
    await emoji_tools.run(["--remap"], bot_factory=lambda token: bot, out=lambda line: None)
    assert emoji_tools.read_map(scan_env / "map.json")["ok"] == "1"


async def test_a_missing_set_or_outage_is_reported_without_the_token(scan_env: Path) -> None:
    bot, session = make_bot()
    lines: list[str] = []
    assert (
        await emoji_tools.run(["--set", "Nope"], bot_factory=lambda t: bot, out=lines.append) == 1
    )
    session.errors.append(RuntimeError("https://api.telegram.org/bot123:SECRET/getStickerSet"))
    assert await emoji_tools.run([], bot_factory=lambda t: bot, out=lines.append) == 1
    assert "SECRET" not in "\n".join(lines)
    assert not (scan_env / "pack.json").exists()  # при сбое файлы не создаются


async def test_missing_settings_are_reported(
    scan_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BOT_TOKEN")
    lines: list[str] = []
    assert await emoji_tools.run([], out=lines.append) == 2
    assert "BOT_TOKEN" in lines[0]


# --- запасной вид при отказе Telegram ---


def guarded_bot(*, premium: bool) -> tuple[object, object, list[str]]:
    bot, session = make_bot()
    session.custom_emoji_allowed = premium
    alerts: list[str] = []

    async def alert(reason: str) -> None:
        alerts.append(reason)

    bot.session.middleware(EmojiFallback(E, alert))
    return bot, session, alerts


async def test_a_refused_message_is_resent_with_plain_emoji_and_the_admin_is_told_once() -> None:
    configured()
    bot, session, alerts = guarded_bot(premium=False)

    await bot.send_message(1, f"{E.ok} Готово")  # тот же путь, что у обычных ответов бота

    sent = session.of(SendMessage)
    assert len(sent) == 2 and "<tg-emoji" in sent[0].text
    assert sent[1].text == "✅ Готово"  # пользователь получил сообщение
    assert not E.active and len(alerts) == 1 and "DOCUMENT_INVALID" in alerts[0]

    await bot.send_message(1, f"{E.ok} Ещё раз")  # дальше сразу обычные, без повторов
    assert len(session.of(SendMessage)) == 3
    assert len(alerts) == 1


async def test_buttons_with_custom_icons_fall_back_too() -> None:
    configured()
    bot, session, alerts = guarded_bot(premium=False)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[E.button("Оплатить", icon=E.ok, callback_data="pay", style="success")]]
    )
    await bot.send_message(1, "Тариф", reply_markup=markup)

    first, second = session.of(SendMessage)
    assert first.reply_markup.inline_keyboard[0][0].icon_custom_emoji_id == IDS["ok"]
    retried = second.reply_markup.inline_keyboard[0][0]
    assert retried.icon_custom_emoji_id is None
    assert retried.text == "✅ Оплатить" and retried.style == "success"
    assert len(alerts) == 1


async def test_edits_fall_back_too() -> None:
    configured()
    bot, session, _ = guarded_bot(premium=False)
    await bot.edit_message_text(f"{E.star} Тариф", chat_id=1, message_id=5)
    first, second = session.of(EditMessageText)
    assert "<tg-emoji" in first.text and second.text == "⭐ Тариф"


async def test_with_premium_nothing_is_retried() -> None:
    configured()
    bot, session, alerts = guarded_bot(premium=True)
    await bot.send_message(1, f"{E.ok} Готово")
    assert len(session.of(SendMessage)) == 1 and E.active and not alerts


async def test_the_flag_off_never_sends_custom_emoji() -> None:
    configured(enabled=False)
    bot, session, alerts = guarded_bot(premium=False)  # даже без Premium всё проходит
    await bot.send_message(1, f"{E.ok} Готово")
    markup = InlineKeyboardMarkup(inline_keyboard=[[E.button("Да", icon=E.ok, callback_data="y")]])
    await bot.send_message(1, "Вопрос", reply_markup=markup)
    assert all("<tg-emoji" not in m.text for m in session.of(SendMessage))
    assert len(session.of(SendMessage)) == 2 and not alerts


async def test_unrelated_refusals_do_not_switch_the_emoji_off() -> None:
    configured()
    bot, session, alerts = guarded_bot(premium=True)
    for message in (
        "Bad Request: message is not modified",
        "Bad Request: message to edit not found",
        "Bad Request: message is too long",
    ):
        session.errors.append(
            TelegramBadRequest(method=SendMessage(chat_id=1, text="x"), message=message)
        )
        with pytest.raises(TelegramBadRequest, match=message.split(": ")[1]):
            await bot.send_message(1, f"{E.ok} Текст")
    assert E.active and not alerts
    assert len(session.of(SendMessage)) == 3  # без повторов


async def test_a_refusal_that_a_plain_retry_does_not_fix_is_not_blamed_on_emoji() -> None:
    configured()
    bot, session, alerts = guarded_bot(premium=True)
    boom = TelegramBadRequest(method=SendMessage(chat_id=1, text="x"), message="Bad Request: boom")
    session.errors.extend([boom, boom])
    with pytest.raises(TelegramBadRequest, match="boom"):
        await bot.send_message(1, f"{E.ok} Текст")
    assert E.active and not alerts and len(session.of(SendMessage)) == 2


async def test_a_refusal_without_custom_emoji_is_passed_through_untouched() -> None:
    configured()
    bot, session, _ = guarded_bot(premium=True)
    boom = TelegramBadRequest(method=SendMessage(chat_id=1, text="x"), message="Bad Request: boom")
    session.errors.append(boom)
    with pytest.raises(TelegramBadRequest):
        await bot.send_message(1, "Обычный текст")
    assert len(session.of(SendMessage)) == 1 and E.active


async def test_probing_shows_the_real_answer_of_telegram() -> None:
    configured()
    bot, session, alerts = guarded_bot(premium=False)
    with probing(), pytest.raises(TelegramBadRequest, match="DOCUMENT_INVALID"):
        await bot.send_message(1, f"{E.ok} Проверка")
    assert E.active and not alerts and len(session.of(SendMessage)) == 1


async def test_a_failing_alert_does_not_lose_the_message() -> None:
    configured()
    bot, session = make_bot()
    session.custom_emoji_allowed = False

    async def broken_alert(reason: str) -> None:
        raise RuntimeError("admin chat is gone")

    bot.session.middleware(EmojiFallback(E, broken_alert))
    await bot.send_message(1, f"{E.ok} Готово")
    assert session.of(SendMessage)[-1].text == "✅ Готово" and not E.active


def test_stripping_handles_every_tag_form() -> None:
    assert (
        strip_custom_emoji(
            '<tg-emoji emoji-id="12">🔒</tg-emoji> и <tg-emoji emoji-id="3">⭐</tg-emoji>'
        )
        == "🔒 и ⭐"
    )
    assert strip_custom_emoji("без эмодзи <b>жирный</b>") == "без эмодзи <b>жирный</b>"


def test_plain_updates_cover_captions_and_media() -> None:
    from aiogram.methods import EditMessageMedia, SendPhoto
    from aiogram.types import InputMediaPhoto

    configured()
    caption = f"{E.ok} Подпись"
    updates = plain_updates(SendPhoto(chat_id=1, photo="file", caption=caption), E)
    assert updates == {"caption": "✅ Подпись"}
    media = InputMediaPhoto(media="file", caption=caption)
    updates = plain_updates(EditMessageMedia(media=media), E)
    assert updates["media"].caption == "✅ Подпись"
    assert plain_updates(SendMessage(chat_id=1, text="обычный"), E) == {}


# --- подключение при запуске ---


async def test_startup_wires_the_map_and_the_fallback(tmp_path: Path) -> None:
    map_file = tmp_path / "map.json"
    map_file.write_text(json.dumps({"ok": IDS["ok"]}), encoding="utf-8")
    bot, session = make_bot()
    session.custom_emoji_allowed = False
    told: list[str] = []

    async def alert(text: str) -> None:
        told.append(text)

    settings = SimpleNamespace(emoji_map_file=map_file, use_custom_emoji=True)
    _setup_emoji(settings, bot, SimpleNamespace(alert=alert))
    assert E.active and E.ok.custom_id == IDS["ok"]

    await bot.send_message(1, f"{E.ok} Готово")
    assert session.of(SendMessage)[-1].text == "✅ Готово"
    assert len(told) == 1 and "USE_CUSTOM_EMOJI=false" in told[0] and "/emoji_preview" in told[0]


async def test_startup_without_a_map_file_is_fine(tmp_path: Path) -> None:
    bot, _ = make_bot()
    settings = SimpleNamespace(emoji_map_file=tmp_path / "missing.json", use_custom_emoji=True)
    _setup_emoji(settings, bot, SimpleNamespace(alert=None))
    assert str(E.ok) == "✅"


# --- /emoji_preview ---


@pytest.fixture
async def h(
    db: Database, clock: FakeClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Harness:
    monkeypatch.setattr(emoji_preview, "PAUSE_S", 0)
    harness = await Harness.build(db, clock)
    items = [(f"{chr(0x1F600 + i)}", str(6000 + i)) for i in range(95)]
    pack = pack_of(*items)
    emoji_tools.write_json(tmp_path / "pack.json", emoji_tools.pack_to_json(pack))
    harness.dp.workflow_data["emoji_pack_file"] = tmp_path / "pack.json"
    return harness


async def test_the_preview_shows_status_mapping_pack_and_buttons(h: Harness) -> None:
    configured()
    await h.say("/emoji_preview", user=ADMIN)
    texts = h.texts()
    assert "<b>Эмодзи</b>" in texts[0]
    assert "USE_CUSTOM_EMOJI" in texts[0] and "95 шт." in texts[0]
    mapping = next(t for t in texts if "Сопоставление" in t and "<code>ok</code>" in t)
    assert f'<tg-emoji emoji-id="{IDS["ok"]}">✅</tg-emoji>' in mapping
    assert f"<code>{IDS['ok']}</code>" in mapping
    assert "<code>shield</code> · не задан" in mapping
    pack_messages = [t for t in texts if t.startswith("<b>Пак,")]
    assert len(pack_messages) == 3  # 40 + 40 + 15
    assert "1–40 из 95" in pack_messages[0] and "81–95 из 95" in pack_messages[2]
    assert (
        '<tg-emoji emoji-id="6000">' in pack_messages[0] and "<code>6094</code>" in pack_messages[2]
    )
    last = h.session.of(SendMessage)[-1]
    buttons = [b for row in last.reply_markup.inline_keyboard for b in row]
    assert {b.style for b in buttons} == {"success", "danger", "primary", None}
    open_button = next(b for b in buttons if b.text == "Открыть")
    assert open_button.icon_custom_emoji_id == IDS["star"]
    assert next(b for b in buttons if b.text == "Оплатить").icon_custom_emoji_id is None
    assert all(b.callback_data == emoji_preview.NOOP for b in buttons)


async def test_the_preview_sends_real_custom_emoji_even_when_the_flag_is_off(h: Harness) -> None:
    configured(enabled=False)
    await h.say("/emoji_preview", user=ADMIN)
    assert "Флаг <code>USE_CUSTOM_EMOJI</code>: выключен" in h.texts()[0]
    assert any("<tg-emoji" in t for t in h.texts())


async def test_without_premium_the_preview_explains_and_does_not_switch_anything(
    h: Harness,
) -> None:
    configured()
    h.session.custom_emoji_allowed = False
    h.bot.session.middleware(EmojiFallback(E, h.alerter.alert))
    await h.say("/emoji_preview", user=ADMIN)
    assert "Telegram отклонил кастомные эмодзи" in h.last().text
    assert "Premium" in h.last().text
    assert E.active and not h.alerter.alerts  # проверка не считается отказом в работе


async def test_a_degraded_bot_offers_to_try_again(h: Harness) -> None:
    configured()
    E.degrade("DOCUMENT_INVALID")
    await h.say("/emoji_preview", user=ADMIN)
    assert "Telegram отклонил кастомные: DOCUMENT_INVALID" in h.texts()[0]
    first = h.session.of(SendMessage)[0]
    assert first.reply_markup.inline_keyboard[0][0].callback_data == emoji_preview.RETRY
    await h.press(emoji_preview.RETRY, user=ADMIN)
    assert E.active
    assert h.session.of(AnswerCallbackQuery)[-1].text == emoji_preview.RETRIED


async def test_a_missing_pack_file_is_explained(h: Harness, tmp_path: Path) -> None:
    h.dp.workflow_data["emoji_pack_file"] = tmp_path / "nope.json"
    await h.say("/emoji_preview", user=ADMIN)
    assert "scan_emoji" in h.last().text


async def test_only_admins_can_use_the_preview(h: Harness) -> None:
    configured()
    await h.say("/emoji_preview", user=USER)
    assert not any("<tg-emoji" in t for t in h.texts())
    assert not any("Пак," in t for t in h.texts())
    await h.press(emoji_preview.RETRY, user=USER)  # кнопка тоже только админам
    assert E.active  # ничего не переключилось
