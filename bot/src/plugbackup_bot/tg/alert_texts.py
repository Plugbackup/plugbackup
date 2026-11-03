"""Тексты алертов безопасности и кнопки под ними."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from plugbackup_bot.db.clients import Client
from plugbackup_bot.domain.alerts import BY_KEY, Finding, threshold_label
from plugbackup_bot.tg.format import fmt_bytes, fmt_datetime, gb
from plugbackup_bot.tg.texts import esc

BTN_ALERT_OFF = "Отключить этот алерт"
BTN_RESTORE = "Запросить восстановление"
CB_ALERT_OFF = "al:off:"
CB_RESTORE = "restore:start"

_CHANGE_KEY = "замените SSH-ключ: /key"


def _markup(key: str, *extra: InlineKeyboardButton) -> InlineKeyboardMarkup:
    off = InlineKeyboardButton(text=BTN_ALERT_OFF, callback_data=f"{CB_ALERT_OFF}{key}")
    return InlineKeyboardMarkup(inline_keyboard=[[button] for button in (*extra, off)])


class TelegramAlertRenderer:
    """Реализует services.alerts.AlertRenderer."""

    def alert(
        self, finding: Finding, client: Client, tz: str
    ) -> tuple[str, InlineKeyboardMarkup | None]:
        data = finding.data
        key = finding.key
        extra: tuple[InlineKeyboardButton, ...] = ()
        match key:
            case "stale":
                text = (
                    f"Занятое место не менялось {data['days']} дн.: новые данные не поступали. "
                    "Проверьте, запускаются ли бэкапы."
                )
            case "drop":
                text = (
                    f"Занятое место за сутки уменьшилось на {data['percent']}%: "
                    f"было {fmt_bytes(int(data['from']))}, стало {fmt_bytes(int(data['to']))}. "
                    "Так бывает, если бэкапы удаляют, в том числе вредоносная программа. "
                    "Если это были не вы, запросите восстановление из снимка: "
                    "снимки хранятся 7 дней."
                )
                extra = (InlineKeyboardButton(text=BTN_RESTORE, callback_data=CB_RESTORE),)
            case "spike":
                label = threshold_label("spike", int(data["threshold"]))
                text = f"За сутки загружено около {fmt_bytes(int(data['rise']))}, это {label}."
            case "fails":
                text = (
                    f"За последний час {data['count']} неудачных попыток входа под вашим "
                    f"логином. Если это были не вы, {_CHANGE_KEY}."
                )
            case "new_ip":
                count = int(data["count"])
                what = (
                    f"с IP-адреса, которого нет в вашей истории за 30 дней (метка {data['label']})"
                    if count == 1
                    else f"с {count} новых IP-адресов (первая метка {data['label']})"
                )
                text = f"К хранилищу подключились {what}. Если это были не вы, {_CHANGE_KEY}."
            case _ if key.startswith("fill_"):
                text = (
                    f"Хранилище заполнено на {data['percent']}%: занято "
                    f"{fmt_bytes(int(data['used']))} из {fmt_bytes(int(data['quota']))}."
                )
                if data["percent"] == 100:
                    text += (
                        " Новые копии не запишутся, пока вы не освободите место "
                        "или не смените тариф: /upgrade."
                    )
            case _:
                text = BY_KEY[key].description
        return text, _markup(key, *extra)

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
        lines = ["Сводка за неделю"]
        if used is None:
            lines.append(f"Занято: данных пока нет, лимит {gb(client.quota_gb)}.")
        else:
            lines.append(f"Занято: {fmt_bytes(used)} из {fmt_bytes(quota)}.")
        if week_delta is not None:
            sign = "+" if week_delta >= 0 else "−"
            lines.append(f"За неделю: {sign}{fmt_bytes(abs(week_delta))}.")
        if last_change is not None:
            lines.append(f"Последнее изменение размера: {fmt_datetime(last_change, tz)}.")
        else:
            lines.append("Размер не менялся за всё время наблюдения.")
        return "\n".join(lines), _markup("weekly")

    def service(
        self, kind: str, text: str, phase: str, client: Client
    ) -> tuple[str, InlineKeyboardMarkup | None]:
        titles = {"maintenance": "Плановые работы", "restore": "Восстановление"}
        if kind == "node_down":
            body = (
                "Нода хранения недоступна больше 15 минут. Резервные копии сейчас не запишутся."
                " Как только нода заработает, пришлём сообщение."
                if phase == "start"
                else "Нода хранения снова доступна."
            )
        else:
            title = titles.get(kind, "Состояние сервиса")
            body = f"{title}: {esc(text)}" if phase == "start" else f"{title} завершены."
        return body, _markup("service")
