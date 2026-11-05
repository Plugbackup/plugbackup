"""Каркас оформления: подстановка, карточки, кнопки, пагинация, баннеры, эффекты, «одно окно»."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    DeleteMessage,
    EditMessageMedia,
    EditMessageText,
    SendMessage,
    SendPhoto,
)
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, PhotoSize, User

from fakes import FakeClock, FakeSession, check_telegram_html, make_bot
from plugbackup_bot.db import Database
from plugbackup_bot.emoji import E
from plugbackup_bot.tg.effect_fallback import EffectFallback
from plugbackup_bot.tg.format import quota_bar
from plugbackup_bot.ui import banners as banner_keys
from plugbackup_bot.ui import buttons, cards, pagination
from plugbackup_bot.ui.banners import BannerStore
from plugbackup_bot.ui.effects import EFFECT_CONFETTI, Effects
from plugbackup_bot.ui.markup import Raw, esc, fill
from plugbackup_bot.ui.screen import Screen, Ui

CHAT = 42
USER = User(id=CHAT, is_bot=False, first_name="Тест")
PNG = b"\x89PNG fake banner bytes"


# --- подстановка и карточки ---


def test_fill_escapes_every_value_but_not_raw_html() -> None:
    result = fill("<b>{who}</b> и {tag}", who="<script>&", tag=Raw("<code>ok</code>"))
    assert result == "<b>&lt;script&gt;&amp;</b> и <code>ok</code>"
    assert isinstance(result, Raw)
    assert esc(result) == str(result)  # готовый HTML второй раз не экранируется
    assert esc(5) == "5" and esc("a<b") == "a&lt;b"


def test_cards_use_one_style() -> None:
    text = cards.card(
        "Доступ готов",
        cards.lines(cards.field("Логин", "u1001"), cards.field("Хост", "node1.example.org")),
        cards.quote("Длинное пояснение."),
        None,
        icon=E.ok,
    )
    assert text == (
        "✅ <b>Доступ готов</b>\n\n"
        "Логин: <code>u1001</code>\nХост: <code>node1.example.org</code>\n\n"
        "<blockquote expandable>Длинное пояснение.</blockquote>"
    )
    check_telegram_html(text)


def test_card_values_are_escaped() -> None:
    text = cards.card("<b>Хитрое</b>", cards.field("Имя", "a<b>&"))
    assert "&lt;b&gt;Хитрое" in text and "a&lt;b&gt;&amp;" in text
    check_telegram_html(text)


def test_bullets_and_plain_fields() -> None:
    assert cards.bullets(["раз", "два"]) == "• раз\n• два"
    assert cards.plain_field("Срок", "2 ноября") == "Срок: 2 ноября"
    assert cards.lines("a", None, "", "b") == "a\nb"


# --- полоска квоты ---


@pytest.mark.parametrize(
    ("used", "quota", "expected"),
    [
        (0, 100, "▱▱▱▱▱▱▱▱▱▱ 0%"),
        (30, 100, "▰▰▰▱▱▱▱▱▱▱ 30%"),
        (1, 1000, "▰▱▱▱▱▱▱▱▱▱ 0%"),  # занято чуть-чуть: одно деление, но проценты честные
        (99, 100, "▰▰▰▰▰▰▰▰▰▱ 99%"),  # полной полоски до 100% не бывает
        (100, 100, "▰▰▰▰▰▰▰▰▰▰ 100%"),
        (120, 100, "▰▰▰▰▰▰▰▰▰▰ 120%"),
        (5, 0, "▰▱▱▱▱▱▱▱▱▱ 0%"),
    ],
)
def test_the_quota_bar(used: int, quota: int, expected: str) -> None:
    assert quota_bar(used, quota) == expected


def test_the_quota_bar_width_is_configurable() -> None:
    assert quota_bar(60, 100, width=5) == "▰▰▰▱▱ 60%"


# --- кнопки ---


def test_a_button_has_exactly_one_action() -> None:
    with pytest.raises(ValueError, match="одно действие"):
        buttons.button("Нет действия")
    with pytest.raises(ValueError, match="одно действие"):
        buttons.button("Два", data="a", url="https://example.org")


def test_a_button_guards_telegram_limits() -> None:
    with pytest.raises(ValueError, match="64"):
        buttons.plain("Длинная", "я" * 33)
    with pytest.raises(ValueError, match="256"):
        buttons.copy_button("Копировать", "x" * 257)
    with pytest.raises(ValueError, match="256"):
        buttons.copy_button("Копировать", "")
    assert buttons.copy_button("Копировать", "x" * 256).copy_text.text == "x" * 256


def test_button_colors_mean_something() -> None:
    assert buttons.success("Оплатить", "a").style == "success"
    assert buttons.danger("Удалить", "b").style == "danger"
    assert buttons.primary("Открыть", "c").style == "primary"
    assert buttons.plain("Ещё", "d").style is None
    assert buttons.link("Сайт", "https://example.org").url == "https://example.org"


def test_copy_button_copies_without_callback() -> None:
    button = buttons.copy_button("Копировать логин", "u1001")
    assert button.copy_text.text == "u1001"
    assert button.callback_data is None and button.url is None


def test_navigation_rows() -> None:
    assert [b.callback_data for b in buttons.nav("m:tariffs")] == ["m:tariffs", buttons.HOME_DATA]
    assert [b.callback_data for b in buttons.nav()] == [buttons.HOME_DATA]
    assert [b.callback_data for b in buttons.nav("x", with_home=False)] == ["x"]


def test_markup_skips_empty_rows() -> None:
    markup = buttons.markup([buttons.plain("A", "a")], [], buttons.nav())
    assert len(markup.inline_keyboard) == 2


# --- пагинация ---


def test_pagination_slices_and_clamps() -> None:
    items = list(range(19))
    first = pagination.paginate(items, 0)
    assert first.items == list(range(8)) and first.pages == 3 and first.first_index == 1
    last = pagination.paginate(items, 99)
    assert last.number == 2 and last.items == [16, 17, 18] and last.first_index == 17
    assert pagination.paginate(items, -5).number == 0
    empty = pagination.paginate([], 3)
    assert empty.items == [] and empty.pages == 1 and empty.number == 0


def test_pager_row_has_prev_label_and_next() -> None:
    page = pagination.paginate(list(range(30)), 1)
    row = pagination.pager(page, "cl:p")
    assert [b.text for b in row] == ["‹", "2 из 4", "›"]
    assert [b.callback_data for b in row] == ["cl:p0", pagination.NOOP_DATA, "cl:p2"]
    assert [b.text for b in pagination.pager(pagination.paginate(list(range(30)), 0), "x")] == [
        "1 из 4",
        "›",
    ]
    assert [b.text for b in pagination.pager(pagination.paginate(list(range(30)), 3), "x")] == [
        "‹",
        "4 из 4",
    ]
    assert pagination.pager(pagination.paginate(list(range(5)), 0), "x") == []


def test_parse_page_survives_garbage() -> None:
    assert pagination.parse_page("3") == 3
    for junk in ("", "-1", "x", "١٢", "9" * 12, "3.5"):
        assert pagination.parse_page(junk) == 0


# --- баннеры ---


@pytest.fixture
def assets(tmp_path: Path) -> Path:
    directory = tmp_path / "assets"
    directory.mkdir()
    (directory / "banner-announce.png").write_bytes(PNG)
    (directory / "banner-update.png").write_bytes(PNG + b"2")
    (directory / "banner-paid.png").write_bytes(PNG + b"3")
    return directory


def test_banner_keys_match_the_design_files() -> None:
    design = Path(__file__).resolve().parents[2] / "design" / "graphics" / "png"
    for key in (banner_keys.WELCOME, banner_keys.TARIFFS, banner_keys.PAID):
        assert (design / f"{key}.png").is_file(), key


async def test_a_banner_is_uploaded_first_then_sent_by_file_id(db: Database, assets: Path) -> None:
    store = BannerStore(db, assets, FakeClock())
    first = await store.media("banner-announce")
    assert first is not None
    digest, source = first
    assert not isinstance(source, str) and source.filename == "banner-announce.png"

    await store.remember(digest, "AgACfileid")
    again = await store.media("banner-announce")
    assert again == (digest, "AgACfileid")

    await store.forget(digest)
    forgotten = await store.media("banner-announce")
    assert forgotten is not None and not isinstance(forgotten[1], str)


async def test_the_cache_follows_the_file_content(db: Database, assets: Path) -> None:
    store = BannerStore(db, assets, FakeClock())
    digest, _ = await store.media("banner-announce")  # type: ignore[misc]
    await store.remember(digest, "old")
    path = assets / "banner-announce.png"
    path.write_bytes(PNG + b"changed")
    # mtime у файла мог не измениться, но размер другой: хеш пересчитывается
    new_digest, source = await store.media("banner-announce")  # type: ignore[misc]
    assert new_digest != digest and not isinstance(source, str)  # новая картинка загрузится заново


async def test_a_missing_banner_is_not_an_error(
    db: Database, assets: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = BannerStore(db, assets, FakeClock())
    assert await store.media("banner-plan-gb999") is None
    assert await store.media("banner-plan-gb999") is None
    assert len([r for r in caplog.records if "gb999" in r.getMessage()]) == 1  # в журнал один раз


async def test_banner_keys_cannot_escape_the_assets_folder(db: Database, assets: Path) -> None:
    (assets.parent / "secret.png").write_bytes(b"x")
    store = BannerStore(db, assets, FakeClock())
    for key in ("../secret", "..\\secret", "a/b", "", "Banner", "x" * 80):
        assert store.find(key) is None


# --- эффекты ---


def test_effects_are_off_when_not_wanted_or_degraded() -> None:
    effects = Effects()
    assert effects.pick(True) == EFFECT_CONFETTI and effects.pick(False) is None
    assert Effects(enabled=False).pick(True) is None
    assert effects.degrade("EFFECT_ID_INVALID") is True and effects.degrade("again") is False
    assert effects.pick(True) is None


# --- «одно окно» ---


def message(session: FakeSession, kind: str, message_id: int = 77) -> Message:
    session.message_kinds[(CHAT, message_id)] = kind
    base = {
        "message_id": message_id,
        "date": datetime.now(UTC),
        "chat": Chat(id=CHAT, type="private"),
    }
    if kind == "photo":
        photo = PhotoSize(file_id="old", file_unique_id="u", width=1, height=1)
        return Message(**base, photo=[photo], caption="старое")
    return Message(**base, text="старое")


def press(bot: Bot, source: Message | None) -> CallbackQuery:
    return CallbackQuery(id="1", from_user=USER, chat_instance="c", data="d", message=source).as_(
        bot
    )


def a_screen(**fields: object) -> Screen:
    markup = InlineKeyboardMarkup(inline_keyboard=[[buttons.plain("Дальше", "next")]])
    return Screen("<b>Экран</b>", markup, **fields)  # type: ignore[arg-type]


@pytest.fixture
def ui_env(db: Database, assets: Path) -> tuple[Bot, FakeSession, Ui, BannerStore]:
    bot, session = make_bot()
    store = BannerStore(db, assets, FakeClock())
    return bot, session, Ui(store, Effects()), store


async def test_a_button_press_edits_the_same_text_message(ui_env) -> None:  # type: ignore[no-untyped-def]
    bot, session, ui, _ = ui_env
    await ui.show(press(bot, message(session, "text")), a_screen())
    [edit] = session.of(EditMessageText)
    assert edit.message_id == 77 and edit.text == "<b>Экран</b>"
    assert not session.of(SendMessage) and not session.of(DeleteMessage)


async def test_a_command_sends_a_new_message(ui_env) -> None:  # type: ignore[no-untyped-def]
    bot, session, ui, _ = ui_env
    command = Message(
        message_id=1, date=datetime.now(UTC), chat=Chat(id=CHAT, type="private"), text="/start"
    ).as_(bot)
    await ui.show(command, a_screen())
    assert len(session.of(SendMessage)) == 1 and not session.of(EditMessageText)


async def test_new_forces_a_fresh_message(ui_env) -> None:  # type: ignore[no-untyped-def]
    bot, session, ui, _ = ui_env
    await ui.show(press(bot, message(session, "text")), a_screen(), new=True)
    assert len(session.of(SendMessage)) == 1 and not session.of(EditMessageText)


async def test_an_inaccessible_message_gets_a_fresh_one(ui_env) -> None:  # type: ignore[no-untyped-def]
    bot, session, ui, _ = ui_env
    await ui.show(press(bot, None), a_screen())
    [sent] = session.of(SendMessage)
    assert sent.chat_id == CHAT


async def test_a_banner_screen_replaces_a_text_message_with_a_photo(ui_env) -> None:  # type: ignore[no-untyped-def]
    bot, session, ui, store = ui_env
    await ui.show(press(bot, message(session, "text")), a_screen(banner="banner-announce"))

    [photo] = session.of(SendPhoto)
    assert photo.caption == "<b>Экран</b>" and not isinstance(photo.photo, str)  # первая загрузка
    [gone] = session.of(DeleteMessage)
    assert gone.message_id == 77  # старый текст убран, чтобы не висел рядом
    assert not session.of(EditMessageText)
    media = await store.media("banner-announce")
    assert media is not None and isinstance(media[1], str)  # file_id запомнен


async def test_the_second_send_uses_the_cached_file_id(ui_env) -> None:  # type: ignore[no-untyped-def]
    bot, session, ui, _ = ui_env
    await ui.send(bot, CHAT, a_screen(banner="banner-announce"))
    await ui.send(bot, CHAT, a_screen(banner="banner-announce"))
    first, second = session.of(SendPhoto)
    assert not isinstance(first.photo, str)
    assert isinstance(second.photo, str) and second.photo.startswith("fid-")


async def test_photo_to_photo_edits_the_media(ui_env) -> None:  # type: ignore[no-untyped-def]
    bot, session, ui, _ = ui_env
    await ui.show(press(bot, message(session, "photo")), a_screen(banner="banner-update"))
    [edit] = session.of(EditMessageMedia)
    assert edit.message_id == 77 and edit.media.caption == "<b>Экран</b>"
    assert not session.of(SendPhoto) and not session.of(DeleteMessage)


async def test_photo_to_text_sends_text_and_removes_the_photo(ui_env) -> None:  # type: ignore[no-untyped-def]
    bot, session, ui, _ = ui_env
    await ui.show(press(bot, message(session, "photo")), a_screen())
    assert len(session.of(SendMessage)) == 1
    assert [m.message_id for m in session.of(DeleteMessage)] == [77]
    assert not session.of(EditMessageText)


async def test_a_missing_banner_file_falls_back_to_text(ui_env) -> None:  # type: ignore[no-untyped-def]
    bot, session, ui, _ = ui_env
    await ui.show(press(bot, message(session, "text")), a_screen(banner="banner-plan-gb999"))
    assert len(session.of(EditMessageText)) == 1 and not session.of(SendPhoto)


async def test_an_unchanged_screen_is_not_an_error(ui_env) -> None:  # type: ignore[no-untyped-def]
    bot, session, ui, _ = ui_env
    source = message(session, "text")
    session.errors.append(
        TelegramBadRequest(
            method=EditMessageText(text="x"), message="Bad Request: message is not modified"
        )
    )
    await ui.show(press(bot, source), a_screen())  # не бросает
    assert len(session.requests) == 1


async def test_a_message_that_cannot_be_edited_is_replaced_by_a_new_one(ui_env) -> None:  # type: ignore[no-untyped-def]
    bot, session, ui, _ = ui_env
    session.errors.append(
        TelegramBadRequest(
            method=EditMessageText(text="x"), message="Bad Request: message to edit not found"
        )
    )
    await ui.show(press(bot, message(session, "text")), a_screen())
    assert len(session.of(SendMessage)) == 1 and not session.of(DeleteMessage)


async def test_other_edit_errors_are_not_hidden(ui_env) -> None:  # type: ignore[no-untyped-def]
    bot, session, ui, _ = ui_env
    session.errors.append(
        TelegramBadRequest(method=EditMessageText(text="x"), message="Bad Request: boom")
    )
    with pytest.raises(TelegramBadRequest, match="boom"):
        await ui.show(press(bot, message(session, "text")), a_screen())
    assert not session.of(SendMessage)  # не маскируем ошибку отправкой дубля


async def test_a_forgotten_file_id_is_uploaded_again(ui_env) -> None:  # type: ignore[no-untyped-def]
    bot, session, ui, store = ui_env
    digest, _ = await store.media("banner-announce")  # type: ignore[misc]
    await store.remember(digest, "bad:stale")  # Telegram такой id не знает
    await ui.send(bot, CHAT, a_screen(banner="banner-announce"))
    stale, fresh = session.of(SendPhoto)
    assert stale.photo == "bad:stale" and not isinstance(fresh.photo, str)
    media = await store.media("banner-announce")
    assert media is not None and media[1] == f"fid-{session.last_message[CHAT][0]}"


async def test_a_forgotten_file_id_is_uploaded_again_on_edit(ui_env) -> None:  # type: ignore[no-untyped-def]
    bot, session, ui, store = ui_env
    digest, _ = await store.media("banner-update")  # type: ignore[misc]
    await store.remember(digest, "bad:stale")
    await ui.show(press(bot, message(session, "photo")), a_screen(banner="banner-update"))
    stale, fresh = session.of(EditMessageMedia)
    assert stale.media.media == "bad:stale" and not isinstance(fresh.media.media, str)


async def test_a_caption_over_1024_is_refused_by_telegram(ui_env) -> None:  # type: ignore[no-untyped-def]
    bot, _, ui, _ = ui_env
    with pytest.raises(TelegramBadRequest, match="caption is too long"):
        await ui.send(bot, CHAT, Screen("я" * 1025, banner="banner-announce"))
    await ui.send(bot, CHAT, Screen("я" * 1024, banner="banner-announce"))


# --- эффекты на новых сообщениях ---


async def test_the_effect_goes_only_on_new_messages(ui_env) -> None:  # type: ignore[no-untyped-def]
    bot, session, ui, _ = ui_env
    await ui.send(bot, CHAT, a_screen(effect=True))
    await ui.send(bot, CHAT, a_screen(effect=True, banner="banner-paid"))
    await ui.send(bot, CHAT, a_screen())
    await ui.show(press(bot, message(session, "text")), a_screen(effect=True))
    sent, photo, plain = (
        session.of(SendMessage)[0],
        session.of(SendPhoto)[0],
        session.of(SendMessage)[1],
    )
    assert sent.message_effect_id == EFFECT_CONFETTI and photo.message_effect_id == EFFECT_CONFETTI
    assert plain.message_effect_id is None
    assert not hasattr(
        session.of(EditMessageText)[0], "message_effect_id"
    )  # правке эффект недоступен


async def test_effects_can_be_turned_off(db: Database, assets: Path) -> None:
    bot, session = make_bot()
    ui = Ui(BannerStore(db, assets, FakeClock()), Effects(enabled=False))
    await ui.send(bot, CHAT, a_screen(effect=True))
    assert session.of(SendMessage)[0].message_effect_id is None


async def test_a_refused_effect_resends_without_it_and_turns_effects_off(
    db: Database, assets: Path
) -> None:
    bot, session = make_bot()
    session.effects_allowed = False
    effects = Effects()
    bot.session.middleware(EffectFallback(effects))
    ui = Ui(BannerStore(db, assets, FakeClock()), effects)

    await ui.send(bot, CHAT, a_screen(effect=True))
    first, retry = session.of(SendMessage)
    assert first.message_effect_id == EFFECT_CONFETTI and retry.message_effect_id is None
    assert not effects.active

    await ui.send(bot, CHAT, a_screen(effect=True))  # дальше сразу без эффекта и без повторов
    assert len(session.of(SendMessage)) == 3


async def test_unrelated_refusals_do_not_turn_effects_off(db: Database, assets: Path) -> None:
    bot, session = make_bot()
    effects = Effects()
    bot.session.middleware(EffectFallback(effects))
    ui = Ui(BannerStore(db, assets, FakeClock()), effects)
    session.errors.append(
        TelegramBadRequest(
            method=SendMessage(chat_id=1, text="x"), message="Bad Request: chat not found"
        )
    )
    with pytest.raises(TelegramBadRequest, match="chat not found"):
        await ui.send(bot, CHAT, a_screen(effect=True))
    assert effects.active and len(session.of(SendMessage)) == 1


# --- сам поддельный Telegram ---


async def test_the_fake_refuses_text_edits_of_a_photo_and_media_edits_of_text() -> None:
    bot, session = make_bot()
    message(session, "photo", 10)
    message(session, "text", 11)
    with pytest.raises(TelegramBadRequest, match="no text"):
        await bot.edit_message_text("x", chat_id=CHAT, message_id=10)
    with pytest.raises(TelegramBadRequest, match="no media"):
        await bot.edit_message_caption(chat_id=CHAT, message_id=11, caption="x")
    await bot.edit_message_text("x", chat_id=CHAT, message_id=11)
    await bot.edit_message_caption(chat_id=CHAT, message_id=10, caption="x")


async def test_the_fake_validates_captions_and_effects() -> None:
    bot, session = make_bot()
    with pytest.raises(TelegramBadRequest, match="can't parse entities"):
        await bot.send_photo(CHAT, "fid", caption="a < b")
    session.effects_allowed = False
    with pytest.raises(TelegramBadRequest, match="EFFECT_ID_INVALID"):
        await bot.send_message(CHAT, "x", message_effect_id=EFFECT_CONFETTI)
