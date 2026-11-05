"""Профиль бота в Telegram: аватар, имя, описания и команды.

Запускается скриптом scripts/setup_profile.py. Картинку описания (640×360, видна в пустом чате)
Bot API ставить не умеет: скрипт готовит файл и печатает шаги для @BotFather.
"""

import argparse
import asyncio
import io
import json
import shutil
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, InputProfilePhotoStatic
from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from plugbackup_bot.commands import ADMIN_COMMANDS, COMMANDS, COMMANDS_EN, set_commands
from plugbackup_bot.config import split_ids

# Лимиты Bot API: имя до 64 знаков, короткое описание до 120, описание до 512.
NAME_MAX = 64
SHORT_DESCRIPTION_MAX = 120
DESCRIPTION_MAX = 512

AVATAR_SIZE = 640
DESCRIPTION_PICTURE_SIZE = (640, 360)
# Графит из design/tokens.json: подложка под прозрачные пиксели аватара.
AVATAR_BACKGROUND = (22, 24, 28)

STEPS = ("photo", "texts", "commands")
_NBSP = " "


class ProfileError(Exception):
    """Понятная пользователю причина, по которой профиль не настроить."""


# --- тексты ---


@dataclass(frozen=True, slots=True)
class ProfileText:
    name: str
    short_description: str
    description: str

    def check(self, language: str) -> None:
        for label, value, limit in (
            ("имя", self.name, NAME_MAX),
            ("короткое описание", self.short_description, SHORT_DESCRIPTION_MAX),
            ("описание", self.description, DESCRIPTION_MAX),
        ):
            if not value.strip() or len(value) > limit:
                raise ProfileError(
                    f"{language}: {label} — {len(value)} знаков, нужно от 1 до {limit}."
                )


def build_texts(beta_gb: int) -> dict[str | None, ProfileText]:
    """Тексты профиля. Ключ None — язык по умолчанию (русский), «en» — английский.

    Объём беты берётся из design/data/plans.json, цен здесь нет: они меняются в админке.
    """
    ru = ProfileText(
        name="PlugBackup",
        short_description=(
            "Хранилище резервных копий: SFTP, restic, rclone. Шифрование на вашей стороне. "
            "Идёт бета."
        ),
        description=(
            "Хранилище резервных копий. Загружайте копии по SFTP через restic или rclone: "
            "данные шифруются на вашем устройстве до отправки, содержимое мы не видим.\n\n"
            f"Сейчас идёт бесплатная бета: {beta_gb}{_NBSP}ГБ по инвайт-коду, один диск без "
            "резервирования. Платные тарифы оплачиваются в Telegram Stars.\n\n"
            "Проект делает один человек. Чтобы начать, нажмите «Старт»."
        ),
    )
    en = ProfileText(
        name="PlugBackup",
        short_description=(
            "Backup storage over SFTP (restic, rclone). Encrypted on your device before upload. "
            "Beta is running."
        ),
        description=(
            "Storage for backups. Upload over SFTP with restic or rclone: data is encrypted on "
            "your device before it is sent, and we cannot see its contents.\n\n"
            f"A free beta is running: {beta_gb}{_NBSP}GB by invite code, a single disk without "
            "redundancy. Paid plans are billed in Telegram Stars.\n\n"
            "The project is run by one person. Press Start to begin."
        ),
    )
    texts: dict[str | None, ProfileText] = {None: ru, "en": en}
    for language, text in texts.items():
        text.check(language or "ru")
    return texts


# --- картинки ---


def _pillow():  # type: ignore[no-untyped-def]
    try:
        from PIL import Image
    except ImportError as error:
        raise ProfileError("Нужен Pillow: pip install -e .[tools]") from error
    return Image


def avatar_jpeg(png_path: Path) -> bytes:
    """Квадратный PNG из design/ в JPG 640×640. Прозрачность заливается графитом."""
    Image = _pillow()
    try:
        with Image.open(png_path) as source:
            image = source.convert("RGBA")
    except OSError as error:
        raise ProfileError(f"Не удалось открыть аватар {png_path}: {error}") from error
    if image.width != image.height:
        raise ProfileError(f"Аватар должен быть квадратным, а в {png_path} {image.size}.")
    if image.size != (AVATAR_SIZE, AVATAR_SIZE):
        image = image.resize((AVATAR_SIZE, AVATAR_SIZE), Image.Resampling.LANCZOS)
    flat = Image.alpha_composite(Image.new("RGBA", image.size, (*AVATAR_BACKGROUND, 255)), image)
    buffer = io.BytesIO()
    flat.convert("RGB").save(buffer, "JPEG", quality=92, optimize=True, subsampling=0)
    return buffer.getvalue()


def check_description_picture(png_path: Path) -> None:
    Image = _pillow()
    try:
        with Image.open(png_path) as image:
            size = image.size
    except OSError as error:
        raise ProfileError(f"Не удалось открыть картинку описания {png_path}: {error}") from error
    if size != DESCRIPTION_PICTURE_SIZE:
        raise ProfileError(
            f"Картинка описания должна быть 640×360, а в {png_path} {size[0]}×{size[1]}."
        )


@dataclass(frozen=True, slots=True)
class PreparedFiles:
    avatar_path: Path | None
    avatar_jpeg: bytes | None
    description_picture: Path | None


def prepare_files(assets_dir: Path, out_dir: Path, steps: Sequence[str]) -> PreparedFiles:
    """Готовит файлы в out_dir: avatar.jpg для Bot API и bot-description.png для @BotFather."""
    out_dir.mkdir(parents=True, exist_ok=True)
    avatar_path = jpeg = description = None
    if "photo" in steps:
        jpeg = avatar_jpeg(assets_dir / "avatar.png")
        avatar_path = out_dir / "avatar.jpg"
        avatar_path.write_bytes(jpeg)
    source = assets_dir / "bot-description.png"
    check_description_picture(source)
    description = out_dir / "bot-description.png"
    shutil.copyfile(source, description)
    return PreparedFiles(avatar_path, jpeg, description)


# --- применение ---


@dataclass(frozen=True, slots=True)
class StepResult:
    step: str
    ok: bool
    message: str


@dataclass(frozen=True, slots=True)
class ProfilePlan:
    texts: dict[str | None, ProfileText]
    photo_jpeg: bytes | None
    admin_ids: tuple[int, ...]


def _describe(error: Exception) -> str:
    """Текст сбоя без адреса запроса: в адресе сетевых ошибок бывает токен бота."""
    if isinstance(error, TelegramAPIError):
        return error.message
    if isinstance(error, ProfileError):
        return str(error)
    return type(error).__name__


async def _guarded(step: str, action: Callable[[], Awaitable[str]]) -> StepResult:
    try:
        return StepResult(step, True, await action())
    except Exception as error:
        return StepResult(step, False, _describe(error))


async def apply_profile(
    bot: Bot, plan: ProfilePlan, steps: Sequence[str] = STEPS
) -> list[StepResult]:
    """Применяет выбранные шаги. Сбой одного шага не мешает остальным."""
    results: list[StepResult] = []

    async def photo() -> str:
        if plan.photo_jpeg is None:
            raise ProfileError("аватар не подготовлен")
        await bot.set_my_profile_photo(
            InputProfilePhotoStatic(photo=BufferedInputFile(plan.photo_jpeg, "avatar.jpg"))
        )
        return "аватар установлен"

    async def texts() -> str:
        for language, text in plan.texts.items():
            await bot.set_my_name(name=text.name, language_code=language)
            await bot.set_my_short_description(
                short_description=text.short_description, language_code=language
            )
            await bot.set_my_description(description=text.description, language_code=language)
        return "имя и описания установлены: " + ", ".join(
            language or "ru (по умолчанию)" for language in plan.texts
        )

    async def commands() -> str:
        report = await set_commands(bot, plan.admin_ids)
        if not report.ok:
            raise RuntimeError("общее меню команд не установилось")
        reached = len(plan.admin_ids) - len(report.admins_without_chat)
        done = f"меню клиентов установлено, меню админов: {reached}"
        if report.admins_without_chat:
            ids = ", ".join(map(str, report.admins_without_chat))
            done += f". Не нашлись чаты {ids}: пусть эти админы напишут боту /start и повторите шаг"
        return done

    for step, action in (("photo", photo), ("texts", texts), ("commands", commands)):
        if step not in steps:
            continue
        results.append(await _guarded(step, action))
    return results


# --- настройки и командная строка ---


class ProfileSettings(BaseSettings):
    """Только то, что нужно скрипту. Читается из того же .env, что и бот."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: SecretStr = Field(min_length=1)
    admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    plans_file: Path = Path("../design/data/plans.json")
    assets_dir: Path = Path("../design/graphics/png")

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _split(cls, value: object) -> object:
        return split_ids(value)


def read_plans_info(path: Path) -> tuple[int, str]:
    """Объём беты в ГБ и @имя бота из design/data/plans.json."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        beta = next(item for item in raw["plans"] if int(item["price_rub"]) == 0)
        return int(beta["gb"]), str(raw["bot"]["handle"])
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as error:
        raise ProfileError(f"Не удалось прочитать тарифы из {path}.") from error


def botfather_steps(username: str, picture: Path | None) -> str:
    handle = username if username.startswith("@") else f"@{username}"
    where = str(picture.resolve()) if picture else "design/graphics/png/bot-description.png"
    return (
        "Картинка описания видна в пустом чате до нажатия «Старт». Bot API её не ставит, "
        "это делается в @BotFather:\n"
        "  1. Откройте чат с @BotFather и отправьте /mybots.\n"
        f"  2. Выберите {handle}, затем Edit Bot → Edit Description Picture.\n"
        f"  3. Пришлите файл {where}\n"
        "     как фото (со сжатием), не как файл.\n"
        "  4. Откройте чат с ботом заново: картинка появится над описанием."
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="setup_profile.py",
        description="Настраивает профиль бота: аватар, имя, описания и меню команд.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="показать, что будет, ничего не меняя"
    )
    parser.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")
    parser.add_argument(
        "--only",
        nargs="+",
        choices=STEPS,
        metavar="ШАГ",
        help=f"только эти шаги: {', '.join(STEPS)}",
    )
    parser.add_argument(
        "--out", type=Path, default=Path("profile_out"), help="куда сложить готовые файлы"
    )
    return parser


def _summary(plan: ProfilePlan, files: PreparedFiles, steps: Sequence[str]) -> str:
    lines: list[str] = []
    if "photo" in steps and files.avatar_path and files.avatar_jpeg:
        lines.append(f"Аватар: {files.avatar_path} ({len(files.avatar_jpeg) // 1024} КБ, 640×640)")
    if "texts" in steps:
        for language, text in plan.texts.items():
            lines += [
                f"Тексты [{language or 'ru, по умолчанию'}]:",
                f"  имя ({len(text.name)}/{NAME_MAX}): {text.name}",
                f"  короткое описание ({len(text.short_description)}/{SHORT_DESCRIPTION_MAX}): "
                f"{text.short_description}",
                f"  описание ({len(text.description)}/{DESCRIPTION_MAX}):",
                *("    " + line for line in text.description.splitlines()),
            ]
    if "commands" in steps:
        lines.append(f"Команды клиентов: {len(COMMANDS)} (ru) и {len(COMMANDS_EN)} (en)")
        lines.append(f"Команды админов: {len(ADMIN_COMMANDS)}, чатов: {len(plan.admin_ids)}")
    return "\n".join(lines)


async def run(
    argv: Sequence[str] | None = None,
    *,
    bot_factory: Callable[[str], Bot] = Bot,
    confirm: Callable[[str], str] = input,
    out: Callable[[str], object] = print,
) -> int:
    """0 — всё применено или показано (--dry-run), 1 — шаг не удался, 2 — не хватает настроек."""
    args = _parser().parse_args(argv)
    steps = tuple(args.only or STEPS)
    try:
        settings = ProfileSettings()  # type: ignore[call-arg]  # значения из окружения и .env
    except ValidationError as error:
        names = sorted({str(item["loc"][0]).upper() for item in error.errors() if item["loc"]})
        out(f"В .env не хватает или неверно: {', '.join(names)}.")
        return 2
    try:
        beta_gb, handle = read_plans_info(settings.plans_file)
        plan_texts = build_texts(beta_gb)
        files = prepare_files(settings.assets_dir, args.out, steps)
    except ProfileError as error:
        out(str(error))
        return 2
    plan = ProfilePlan(plan_texts, files.avatar_jpeg, tuple(settings.admin_ids))

    out(_summary(plan, files, steps))
    out(f"Картинка описания для @BotFather: {files.description_picture}")
    if args.dry_run:
        out("\nРежим --dry-run: в Telegram ничего не отправлено.\n")
        out(botfather_steps(handle, files.description_picture))
        return 0

    bot = bot_factory(settings.bot_token.get_secret_value())
    try:
        try:
            me = await bot.get_me()
        except Exception as error:
            out(f"Не удалось подключиться к Telegram ({_describe(error)}). Проверьте BOT_TOKEN.")
            return 1
        username = me.username or handle
        if not args.yes:
            answer = confirm(f"\nПрименить к @{username}? Профиль станет виден всем. [y/N] ")
            if answer.strip().lower() not in ("y", "yes", "д", "да"):
                out("Отменено, ничего не изменено.")
                return 0
        results = await apply_profile(bot, plan, steps)
    finally:
        await bot.session.close()

    for result in results:
        out(f"{'OK ' if result.ok else 'ОШИБКА'} {result.step}: {result.message}")
    out("")
    out(botfather_steps(username, files.description_picture))
    return 0 if all(result.ok for result in results) else 1


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(run(argv))
