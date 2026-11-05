"""Профиль бота: тексты, аватар, шаги и командная строка scripts/setup_profile.py."""

import io
import subprocess
import sys
from pathlib import Path

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    GetMe,
    SetMyCommands,
    SetMyDescription,
    SetMyName,
    SetMyProfilePhoto,
    SetMyShortDescription,
)
from aiogram.types import BotCommandScopeChat
from PIL import Image

from fakes import make_bot
from plugbackup_bot import profile
from plugbackup_bot.profile import ProfileError, ProfilePlan, ProfileText

BOT_DIR = Path(__file__).resolve().parents[1]
ASSETS = BOT_DIR.parent / "design" / "graphics" / "png"
PLANS = BOT_DIR.parent / "design" / "data" / "plans.json"


def png_bytes(size: tuple[int, int], color: tuple[int, int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", size, color).save(buffer, "PNG")
    return buffer.getvalue()


def a_plan(*, photo: bool = True, admins: tuple[int, ...] = (900, 901)) -> ProfilePlan:
    jpeg = profile.avatar_jpeg(ASSETS / "avatar.png") if photo else None
    return ProfilePlan(profile.build_texts(10), jpeg, admins)


# --- тексты ---


@pytest.mark.parametrize("language", [None, "en"])
def test_profile_texts_fit_the_limits_and_the_brand_rules(language: str | None) -> None:
    text = profile.build_texts(10)[language]
    assert len(text.name) <= profile.NAME_MAX
    assert len(text.short_description) <= profile.SHORT_DESCRIPTION_MAX
    assert len(text.description) <= profile.DESCRIPTION_MAX
    for value in (text.name, text.short_description, text.description):
        assert "!" not in value  # brand.md: без восклицательных знаков
        assert all(ord(ch) < 0x2190 for ch in value)  # и без эмодзи
    assert "10 " in text.description  # число и единица не разрываются


def test_the_beta_size_comes_from_the_argument() -> None:
    assert "25 ГБ" in profile.build_texts(25)[None].description
    assert "25 GB" in profile.build_texts(25)["en"].description


def test_a_too_long_text_is_refused() -> None:
    with pytest.raises(ProfileError, match="имя"):
        ProfileText(name="x" * 65, short_description="ok", description="ok").check("ru")
    with pytest.raises(ProfileError, match="описание"):
        ProfileText(name="x", short_description="ok", description="x" * 513).check("en")


def test_plans_info_is_read_from_the_design_file() -> None:
    beta_gb, handle = profile.read_plans_info(PLANS)
    assert beta_gb == 10
    assert handle == "@PlugBackupBot"
    with pytest.raises(ProfileError):
        profile.read_plans_info(PLANS.parent / "missing.json")


# --- картинки ---


def test_the_avatar_becomes_a_square_jpeg() -> None:
    data = profile.avatar_jpeg(ASSETS / "avatar.png")
    assert data.startswith(bytes([0xFF, 0xD8, 0xFF]))
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (640, 640)
        assert image.mode == "RGB"


def test_a_transparent_avatar_is_filled_with_graphite(tmp_path: Path) -> None:
    source = tmp_path / "clear.png"
    source.write_bytes(png_bytes((640, 640), (0, 0, 0, 0)))
    with Image.open(io.BytesIO(profile.avatar_jpeg(source))) as image:
        red, green, blue = image.getpixel((10, 10))
    assert abs(red - 22) <= 3 and abs(green - 24) <= 3 and abs(blue - 28) <= 3


def test_a_smaller_square_avatar_is_scaled_up(tmp_path: Path) -> None:
    source = tmp_path / "small.png"
    source.write_bytes(png_bytes((320, 320), (200, 200, 200, 255)))
    with Image.open(io.BytesIO(profile.avatar_jpeg(source))) as image:
        assert image.size == (640, 640)


def test_a_non_square_avatar_is_refused(tmp_path: Path) -> None:
    source = tmp_path / "wide.png"
    source.write_bytes(png_bytes((640, 360), (0, 0, 0, 255)))
    with pytest.raises(ProfileError, match="квадрат"):
        profile.avatar_jpeg(source)


def test_the_description_picture_must_be_640_by_360(tmp_path: Path) -> None:
    profile.check_description_picture(ASSETS / "bot-description.png")
    wrong = tmp_path / "wrong.png"
    wrong.write_bytes(png_bytes((100, 100), (0, 0, 0, 255)))
    with pytest.raises(ProfileError, match="640×360"):
        profile.check_description_picture(wrong)


def test_prepared_files_are_written_for_botfather(tmp_path: Path) -> None:
    files = profile.prepare_files(ASSETS, tmp_path / "out", profile.STEPS)
    assert files.avatar_path is not None and files.avatar_path.read_bytes() == files.avatar_jpeg
    assert files.description_picture is not None
    assert files.description_picture.read_bytes() == (ASSETS / "bot-description.png").read_bytes()


# --- применение ---


async def test_every_part_of_the_profile_is_set() -> None:
    bot, session = make_bot()
    results = await profile.apply_profile(bot, a_plan())

    assert [(r.step, r.ok) for r in results] == [
        ("photo", True),
        ("texts", True),
        ("commands", True),
    ]
    assert len(session.of(SetMyProfilePhoto)) == 1
    names = session.of(SetMyName)
    assert [m.language_code for m in names] == [None, "en"]
    assert [m.language_code for m in session.of(SetMyDescription)] == [None, "en"]
    assert [m.language_code for m in session.of(SetMyShortDescription)] == [None, "en"]
    menus = session.of(SetMyCommands)
    admin_scopes = [m.scope.chat_id for m in menus if isinstance(m.scope, BotCommandScopeChat)]
    assert admin_scopes == [900, 901]
    assert len(menus) == 4  # ru и en для всех, плюс по меню на каждого админа


async def test_only_the_chosen_steps_run() -> None:
    bot, session = make_bot()
    results = await profile.apply_profile(bot, a_plan(), steps=("texts",))
    assert [r.step for r in results] == ["texts"]
    assert not session.of(SetMyProfilePhoto) and not session.of(SetMyCommands)


async def test_a_failing_step_does_not_stop_the_others() -> None:
    bot, session = make_bot()
    refusal = TelegramBadRequest(
        method=SetMyProfilePhoto.model_construct(), message="Bad Request: boom"
    )
    session.errors.append(refusal)  # первым идёт аватар
    results = await profile.apply_profile(bot, a_plan())
    assert [r.ok for r in results] == [False, True, True]
    assert "boom" in results[0].message
    assert len(session.of(SetMyName)) == 2


async def test_network_errors_do_not_leak_the_bot_token() -> None:
    bot, session = make_bot()
    session.errors.append(RuntimeError("GET https://api.telegram.org/bot123:SECRET/setMyName"))
    results = await profile.apply_profile(bot, a_plan(photo=False), steps=("texts",))
    assert results[0].ok is False
    assert results[0].message == "RuntimeError"


async def test_an_admin_without_a_chat_is_a_warning_not_a_failure() -> None:
    bot, session = make_bot()
    refusal = TelegramBadRequest(
        method=SetMyCommands(commands=[]), message="Bad Request: chat not found"
    )
    session.errors.extend([None, None, refusal])  # общие меню прошли, админа Telegram не знает
    results = await profile.apply_profile(
        bot, a_plan(photo=False, admins=(900,)), steps=("commands",)
    )
    assert results[0].ok is True
    assert "900" in results[0].message and "/start" in results[0].message


async def test_a_failed_general_menu_is_a_failure() -> None:
    bot, session = make_bot()
    refusal = TelegramBadRequest(method=SetMyCommands(commands=[]), message="Bad Request: boom")
    session.errors.append(refusal)
    results = await profile.apply_profile(bot, a_plan(photo=False, admins=()), steps=("commands",))
    assert results[0].ok is False


# --- командная строка ---


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.chdir(tmp_path)  # настоящий bot/.env не читается
    monkeypatch.setenv("BOT_TOKEN", "123456789:" + "x" * 35)
    monkeypatch.setenv("ADMIN_IDS", "900,901")
    monkeypatch.setenv("PLANS_FILE", str(PLANS))
    monkeypatch.setenv("ASSETS_DIR", str(ASSETS))
    return tmp_path / "out"


async def test_dry_run_changes_nothing_and_prepares_files(cli_env: Path) -> None:
    lines: list[str] = []

    def no_bot(token: str):  # type: ignore[no-untyped-def]
        raise AssertionError("в режиме --dry-run бот не создаётся")

    code = await profile.run(
        ["--dry-run", "--out", str(cli_env)], bot_factory=no_bot, out=lines.append
    )
    text = "\n".join(lines)
    assert code == 0
    assert (cli_env / "avatar.jpg").is_file() and (cli_env / "bot-description.png").is_file()
    assert "Edit Description Picture" in text and "@PlugBackupBot" in text
    assert "dry-run" in text
    assert "xxxxxxxxxx" not in text  # токен не печатается


async def test_the_run_applies_the_profile_after_confirmation(cli_env: Path) -> None:
    bot, session = make_bot()
    lines: list[str] = []
    code = await profile.run(
        ["--out", str(cli_env)],
        bot_factory=lambda token: bot,
        confirm=lambda prompt: "y",
        out=lines.append,
    )
    assert code == 0
    assert session.of(GetMe) and session.of(SetMyProfilePhoto) and len(session.of(SetMyName)) == 2
    assert any(line.startswith("OK") for line in lines)


async def test_declining_the_confirmation_changes_nothing(cli_env: Path) -> None:
    bot, session = make_bot()
    code = await profile.run(
        ["--out", str(cli_env)],
        bot_factory=lambda token: bot,
        confirm=lambda prompt: "",
        out=lambda line: None,
    )
    assert code == 0
    assert not session.of(SetMyName) and not session.of(SetMyProfilePhoto)


async def test_the_exit_code_reports_a_failed_step(cli_env: Path) -> None:
    bot, session = make_bot()
    session.errors.extend(
        [None, TelegramBadRequest(method=SetMyProfilePhoto.model_construct(), message="x")]
    )
    code = await profile.run(
        ["--yes", "--out", str(cli_env)], bot_factory=lambda token: bot, out=lambda line: None
    )
    assert code == 1  # get_me прошёл, аватар отклонён


async def test_a_missing_token_is_explained_without_a_traceback(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BOT_TOKEN")
    lines: list[str] = []
    assert await profile.run(["--dry-run"], out=lines.append) == 2
    assert "BOT_TOKEN" in lines[0]


async def test_a_telegram_outage_is_reported(cli_env: Path) -> None:
    bot, session = make_bot()
    session.errors.append(RuntimeError("https://api.telegram.org/bot123:SECRET/getMe"))
    lines: list[str] = []
    code = await profile.run(
        ["--yes", "--out", str(cli_env)], bot_factory=lambda token: bot, out=lines.append
    )
    assert code == 1
    assert "SECRET" not in "\n".join(lines)


def test_the_script_starts_and_shows_help() -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(BOT_DIR / "scripts" / "setup_profile.py"), "--help"],
        capture_output=True, text=True, encoding="utf-8", cwd=BOT_DIR, check=False,
    )  # fmt: skip
    assert result.returncode == 0
    assert "--dry-run" in result.stdout
