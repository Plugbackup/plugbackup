"""Сообщения и кнопки обязательных уведомлений жизненного цикла."""

from aiogram.types import InlineKeyboardMarkup

from plugbackup_bot import texts_ru as T
from plugbackup_bot.db.clients import Client
from plugbackup_bot.domain.lifecycle import Notice, NoticeKind
from plugbackup_bot.emoji import E
from plugbackup_bot.tg import keyboards
from plugbackup_bot.tg.format import fmt_date
from plugbackup_bot.ui import cards
from plugbackup_bot.ui.markup import fill

# После удаления и сгоревшей беты кнопка не нужна; напоминания о ключе тоже без кнопок.
_NO_BUTTON = frozenset(
    {
        NoticeKind.KEY_REMINDER_1,
        NoticeKind.KEY_REMINDER_3,
        NoticeKind.BETA_EXPIRED,
        NoticeKind.DELETED,
    }
)
# Предупреждения: в заголовке предупреждающий значок, у остальных спокойный.
_WARNING = frozenset(
    {
        NoticeKind.WARN_3D,
        NoticeKind.WARN_1D,
        NoticeKind.RENEWAL_LATE,
        NoticeKind.SUSPEND_SOON,
        NoticeKind.DELETE_SOON,
    }
)
_ERROR = frozenset({NoticeKind.SUSPENDED, NoticeKind.DELETED, NoticeKind.BETA_EXPIRED})


class TelegramNoticeRenderer:
    def __init__(self, tz: str) -> None:
        self._tz = tz

    def render(self, notice: Notice, client: Client) -> tuple[str, InlineKeyboardMarkup | None]:
        date = fmt_date(notice.at, self._tz) if notice.at is not None else "—"
        title, body = self._text(notice, date)
        icon = (
            E.warning if notice.kind in _WARNING else E.error if notice.kind in _ERROR else E.bell
        )
        text = cards.card(title, body, icon=icon)
        if notice.kind in _NO_BUTTON:
            return text, None
        return text, keyboards.alive(client.id) if notice.is_beta else keyboards.to_tariffs()

    @staticmethod
    def _text(notice: Notice, date: str) -> tuple[str, str]:
        beta = notice.is_beta
        kind = notice.kind
        # У бета-клиента напоминания о сроке свои: нажать кнопку, а не оплатить.
        key = "warn_beta" if beta and kind in (NoticeKind.WARN_3D, NoticeKind.WARN_1D) else kind
        title, template = T.NOTICES[key]
        body = fill(
            template,
            date=date,
            key_hint=T.NOTICE_KEY_HINT,
            tail=T.NOTICE_KEY_WEEK_TAIL if beta and kind is NoticeKind.KEY_REMINDER_3 else "",
            return_hint=T.NOTICE_BETA_RETURN if beta else T.NOTICE_PAID_RETURN,
        )
        return title, body
