"""Тексты и кнопки обязательных уведомлений жизненного цикла."""

from aiogram.types import InlineKeyboardMarkup

from plugbackup_bot.db.clients import Client
from plugbackup_bot.domain.lifecycle import Notice, NoticeKind
from plugbackup_bot.tg import keyboards
from plugbackup_bot.tg.format import fmt_date

_KEY_HINT = "Пришлите публичный ключ ssh-ed25519 одной строкой, подсказка — в /key."
_BETA_RETURN = "Нажмите «Я ещё пользуюсь», чтобы сохранить доступ."
_PAID_RETURN = "Чтобы вернуть доступ, оформите подписку в разделе «Тарифы»."


class TelegramNoticeRenderer:
    def __init__(self, tz: str) -> None:
        self._tz = tz

    def render(self, notice: Notice, client: Client) -> tuple[str, InlineKeyboardMarkup | None]:
        date = fmt_date(notice.at, self._tz) if notice.at is not None else "—"
        text = self._text(notice, date)
        if notice.kind in _NO_BUTTON:
            return text, None
        return text, keyboards.alive(client.id) if notice.is_beta else keyboards.to_tariffs()

    @staticmethod
    def _text(notice: Notice, date: str) -> str:
        beta = notice.is_beta
        match notice.kind:
            case NoticeKind.KEY_REMINDER_1:
                return f"Хранилище ждёт ваш ключ. {_KEY_HINT}"
            case NoticeKind.KEY_REMINDER_3:
                tail = " Если ключ не прислать в течение недели, хранилище будет отменено."
                return f"Хранилище всё ещё ждёт ваш ключ. {_KEY_HINT}" + (tail if beta else "")
            case NoticeKind.BETA_EXPIRED:
                return (
                    "Ключ не пришёл за 7 дней, хранилище отменено, а код беты сгорел. "
                    "Запросите новый инвайт-код."
                )
            case NoticeKind.BETA_PROMPT:
                return (
                    "Вы ещё пользуетесь хранилищем? Нажмите кнопку до "
                    f"{date}, иначе оно будет заблокировано."
                )
            case NoticeKind.WARN_3D | NoticeKind.WARN_1D if beta:
                return (
                    f"Напоминание: нажмите «Я ещё пользуюсь» до {date}, "
                    "иначе хранилище будет заблокировано."
                )
            case NoticeKind.WARN_3D:
                return (
                    f"Доступ закончится {date}, продление отключено. "
                    "Чтобы сохранить хранилище, оформите подписку заново в разделе «Тарифы»."
                )
            case NoticeKind.WARN_1D:
                return (
                    f"Доступ закончится завтра, {date}, продление отключено. "
                    "Чтобы сохранить хранилище, оформите подписку заново в разделе «Тарифы»."
                )
            case NoticeKind.RENEWAL_LATE:
                return (
                    "Продление ещё не прошло. Если оплата не поступит до "
                    f"{date}, хранилище будет заблокировано."
                )
            case NoticeKind.SUSPEND_SOON:
                return f"Завтра, {date}, хранилище будет заблокировано. " + (
                    _BETA_RETURN if beta else _PAID_RETURN
                )
            case NoticeKind.SUSPENDED:
                return (
                    "Хранилище заблокировано, доступ по SFTP закрыт. "
                    f"Данные сохранятся до {date}, затем будут удалены. "
                    + (_BETA_RETURN if beta else _PAID_RETURN)
                )
            case NoticeKind.DELETE_SOON:
                return (
                    f"Завтра, {date}, данные хранилища будут удалены без возможности "
                    "восстановления. " + (_BETA_RETURN if beta else _PAID_RETURN)
                )
            case NoticeKind.DELETED:
                return "Хранилище удалено вместе с данными. Начать заново можно через /start."
        return ""


# После удаления и сгоревшей беты кнопка не нужна; напоминания о ключе тоже без кнопок.
_NO_BUTTON = frozenset(
    {
        NoticeKind.KEY_REMINDER_1,
        NoticeKind.KEY_REMINDER_3,
        NoticeKind.BETA_EXPIRED,
        NoticeKind.DELETED,
    }
)
