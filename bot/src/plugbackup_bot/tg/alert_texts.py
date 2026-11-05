"""Сообщения об алертах безопасности, сводка и служебные сообщения с кнопками."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from plugbackup_bot import texts_ru as T
from plugbackup_bot.db.clients import Client
from plugbackup_bot.domain.alerts import BY_KEY, Finding, threshold_label
from plugbackup_bot.emoji import E
from plugbackup_bot.tg.format import fmt_bytes, fmt_datetime, gb
from plugbackup_bot.ui import buttons, cards
from plugbackup_bot.ui.buttons import markup
from plugbackup_bot.ui.markup import esc, fill

BTN_ALERT_OFF = T.BTN_ALERT_OFF
BTN_RESTORE = T.BTN_ALERT_RESTORE
CB_ALERT_OFF = "al:off:"
CB_RESTORE = "restore:start"


def _markup(key: str, *extra: InlineKeyboardButton) -> InlineKeyboardMarkup:
    off = buttons.plain(BTN_ALERT_OFF, f"{CB_ALERT_OFF}{key}", E.bell)
    return markup(*([button] for button in (*extra, off)))


class TelegramAlertRenderer:
    """Реализует services.alerts.AlertRenderer."""

    def alert(
        self, finding: Finding, client: Client, tz: str
    ) -> tuple[str, InlineKeyboardMarkup | None]:
        data = finding.data
        key = finding.key
        extra: tuple[InlineKeyboardButton, ...] = ()
        title, body = "", ""
        change_key = T.ALERT_CHANGE_KEY
        match key:
            case "stale":
                title, template = T.ALERT_STALE
                body = fill(template, days=data["days"])
            case "drop":
                title, template = T.ALERT_DROP
                body = fill(
                    template,
                    percent=data["percent"],
                    before=fmt_bytes(int(data["from"])),
                    after=fmt_bytes(int(data["to"])),
                )
                extra = (buttons.primary(BTN_RESTORE, CB_RESTORE, E.refresh),)
            case "spike":
                title, template = T.ALERT_SPIKE
                label = threshold_label("spike", int(data["threshold"]))
                body = fill(template, rise=fmt_bytes(int(data["rise"])), label=label)
            case "fails":
                title, template = T.ALERT_FAILS
                body = fill(template, count=data["count"], change_key=change_key)
            case "new_ip":
                title, template = T.ALERT_NEW_IP
                count = int(data["count"])
                what = fill(
                    T.ALERT_NEW_IP_ONE if count == 1 else T.ALERT_NEW_IP_MANY,
                    label=data["label"],
                    count=count,
                )
                body = fill(template, what=what, change_key=change_key)
            case _ if key.startswith("fill_"):
                title = T.ALERT_FILL_TITLE.format(percent=data["percent"])
                body = fill(
                    T.ALERT_FILL_BODY,
                    used=fmt_bytes(int(data["used"])),
                    quota=fmt_bytes(int(data["quota"])),
                )
                if data["percent"] == 100:
                    body = body + T.ALERT_FILL_FULL
            case _:
                title, body = BY_KEY[key].title, BY_KEY[key].description
        return cards.card(title, body, icon=E.warning), _markup(key, *extra)

    def digest(
        self,
        client: Client,
        tz: str,
        *,
        used: int | None,
        quota: int,
        week_delta: int | None,
        last_change: int | None,
    ) -> tuple[str, InlineKeyboardMarkup | None]:
        rows: list[str] = []
        if used is None:
            rows.append(fill(T.DIGEST_NO_DATA, quota=gb(client.quota_gb)))
        else:
            rows.append(fill(T.DIGEST_USED, used=fmt_bytes(used), quota=fmt_bytes(quota)))
        if week_delta is not None:
            sign = "+" if week_delta >= 0 else "−"
            rows.append(fill(T.DIGEST_WEEK, sign=sign, delta=fmt_bytes(abs(week_delta))))
        if last_change is not None:
            rows.append(fill(T.DIGEST_LAST_CHANGE, at=fmt_datetime(last_change, tz)))
        else:
            rows.append(T.DIGEST_NO_CHANGE)
        return cards.card(T.DIGEST_TITLE, cards.lines(*rows), icon=E.chart), _markup("weekly")

    def service(
        self, kind: str, text: str, phase: str, client: Client
    ) -> tuple[str, InlineKeyboardMarkup | None]:
        if kind == "node_down":
            if phase == "start":
                title, body = T.SERVICE_NODE_TITLE, T.SERVICE_NODE_DOWN
            else:
                title, body = T.SERVICE_NODE_UP_TITLE, T.SERVICE_NODE_UP
        else:
            title = T.SERVICE_TITLES.get(kind, T.SERVICE_DEFAULT_TITLE)
            body = esc(text) if phase == "start" else fill(T.SERVICE_END, title=title)
        return cards.card(title, body, icon=E.warning), _markup("service")
