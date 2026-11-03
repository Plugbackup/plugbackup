"""Тексты бота. Тон по design/brand.md: «вы», без эмодзи и восклицательных знаков.

Сообщения уходят с parse_mode=HTML, поэтому всё подставляемое экранируется через `esc`.
Тексты для pre_checkout (`REJECTIONS`) показываются Telegram как обычный текст.
"""

import html
from collections.abc import Sequence

from plugbackup_bot.db.clients import Client
from plugbackup_bot.db.nodes import Node
from plugbackup_bot.domain.keys import KeyProblem
from plugbackup_bot.domain.plans import GIB, Plan
from plugbackup_bot.domain.policy import (
    BETA_CONFIRM_PERIOD_S,
    BETA_GRACE_S,
    PAID_GRACE_S,
    SUSPENDED_TTL_S,
)
from plugbackup_bot.domain.states import State
from plugbackup_bot.services.access import RedeemKind, StatusView
from plugbackup_bot.services.billing import Reject
from plugbackup_bot.tg.format import NBSP, fmt_bytes, fmt_date, fmt_datetime, gb

# Путь репозитория на ноде внутри chroot клиента.
REPO_PATH = "/data/restic"
SSH_ALIAS = "plugbackup"


def esc(value: object) -> str:
    return html.escape(str(value))


def _days(seconds: int) -> int:
    return seconds // (24 * 3600)


# --- меню и справка ---

BTN_BETA = "Бета по инвайту"
BTN_TARIFFS = "Тарифы"
BTN_HOW = "Как это работает"
BTN_STATUS = "Статус"


def start(has_client: bool) -> str:
    text = (
        "PlugBackup — хранилище для резервных копий. Копии отправляются по SFTP через restic "
        "или rclone и шифруются на вашем устройстве до отправки, содержимое мы не видим.\n\n"
        "Сейчас бета: один диск, без резервирования. Бета бесплатна, "
        f"10{NBSP}ГБ по инвайт-коду. Платные тарифы — в разделе «Тарифы»."
    )
    if has_client:
        text += "\n\nХранилище у вас уже есть: /status."
    return text


HOW_IT_WORKS = (
    "1. Выберите тариф или введите инвайт-код беты.\n"
    "2. Пришлите публичный ключ ssh-ed25519.\n"
    "3. Получите хост и логин и настройте restic или rclone.\n\n"
    "Данные шифруются на вашем устройстве до отправки. Мы видим размер хранилища, число и "
    "размеры зашифрованных объектов, время их изменения и IP-адрес, с которого вы "
    "подключаетесь. Если это важно, подключайтесь через свой VPN.\n\n"
    "Пароль шифрования у нас не хранится. Если вы его потеряете, восстановить данные будет "
    "нельзя."
)

HELP = (
    "/start — о сервисе\n"
    "/status — занято и лимит, тариф, дата списания\n"
    "/upgrade — сменить тариф\n"
    "/key — заменить SSH-ключ\n"
    "/alerts — настройки уведомлений\n"
    "/restore — запросить восстановление из снимка\n"
    "/rules — правила\n"
    "/help — эта справка"
)


def rules() -> str:
    return (
        "Правила\n\n"
        "• Бета: один диск, без резервирования. Возможны простои и потеря данных.\n"
        f"• Бета бесплатна, раз в {_days(BETA_CONFIRM_PERIOD_S)} дней бот просит нажать "
        f"«Я ещё пользуюсь». Если не нажать за {_days(BETA_GRACE_S)} дней, хранилище блокируется, "
        f"ещё через {_days(SUSPENDED_TTL_S)} дней данные удаляются.\n"
        f"• Платная подписка продлевается каждые 30 дней. Если продление не прошло, "
        f"после окончания периода есть {_days(PAID_GRACE_S)} дня, затем хранилище блокируется, "
        f"а через {_days(SUSPENDED_TTL_S)} дней данные удаляются.\n"
        "• Уменьшить лимит ниже занятого места нельзя.\n"
        "• Ежедневные снимки диска хранятся 7 дней. Восстановление делает администратор по "
        "заявке (/restore), автоматического отката нет.\n"
        "• Храните только то, что вы вправе хранить."
    )


def tariffs(plans: Sequence[Plan], current: Client | None) -> str:
    lines = ["Тарифы", ""]
    for plan in plans:
        lines.append(f"{esc(plan.name)} — {plan.price_rub}{NBSP}₽/мес ({plan.stars}{NBSP}Stars)")
    lines += [
        "",
        "Оплата в Telegram Stars, сумма в рублях — ориентир. Подписка продлевается каждые "
        "30 дней, продление можно отключить в любой момент.",
        "Бета: один диск, без резервирования.",
    ]
    if current is not None:
        lines += ["", f"Сейчас у вас {gb(current.quota_gb)}."]
    return "\n".join(lines)


# --- риски беты ---

# Версию увеличиваем при любой правке текста: клиентам придётся согласиться заново.
RISK_TEXT_VERSION = 1
RISK_TEXT = (
    "Перед оплатой — о рисках беты.\n\n"
    "• Данные лежат на одном диске, без резервирования.\n"
    "• Возможны простои и потеря данных.\n"
    "• Копии шифруются на вашем устройстве, пароль шифрования у нас не хранится. "
    "Если вы его потеряете, восстановить данные будет нельзя.\n\n"
    "Храните вторую копию важных данных в другом месте."
)
BTN_RISK_OK = "Я понимаю риски"

# --- инвайт беты ---

BETA_ASK_CODE = "Пришлите инвайт-код беты."
TOO_MANY_CODES = "Слишком много неверных кодов. Попробуйте через несколько минут."

INVITE_ERRORS: dict[RedeemKind, str] = {
    RedeemKind.INVALID: "Код не подошёл. Проверьте, что он введён без ошибок.",
    RedeemKind.USED: "Этот код уже использован.",
    RedeemKind.ALREADY_BETA: (
        "Бета выдаётся один раз на аккаунт, и вы её уже получали. "
        "Платные тарифы — в разделе «Тарифы»."
    ),
    RedeemKind.HAS_CLIENT: "У вас уже есть хранилище, его состояние — /status.",
    RedeemKind.NO_CAPACITY: "Свободных мест в бете сейчас нет. Попробуйте позже.",
    RedeemKind.NEED_ACK: "Сначала подтвердите, что понимаете риски беты: /start.",
}


def beta_accepted(plan: Plan) -> str:
    return f"Код принят. Бета: {gb(plan.gb)}.\n\n{KEY_HELP}"


# --- ключ ---

KEY_HELP = (
    "Пришлите публичный ключ ssh-ed25519 одной строкой.\n\n"
    "Если ключа нет, создайте его на своём устройстве:\n"
    "<code>ssh-keygen -t ed25519 -f ~/.ssh/plugbackup</code>\n"
    "и покажите публичную часть:\n"
    "<code>cat ~/.ssh/plugbackup.pub</code>\n\n"
    "Приватный ключ, файл без .pub, никому не отправляйте."
)

KEY_REPLACE_HELP = (
    "Пришлите новый публичный ключ ssh-ed25519 одной строкой. "
    "Прежний ключ перестанет работать, когда нода применит замену.\n\n"
    "Создать ключ: <code>ssh-keygen -t ed25519 -f ~/.ssh/plugbackup</code>"
)

KEY_PROBLEMS: dict[KeyProblem, str] = {
    KeyProblem.EMPTY: "Пришлите ключ одной строкой.",
    KeyProblem.TOO_LONG: "Слишком длинное сообщение. Нужен один публичный ключ, около 80 символов.",
    KeyProblem.PRIVATE_KEY: (
        "Это приватный ключ. Не отправляйте его никому: создайте новую пару ключей, "
        "а этот считайте скомпрометированным."
    ),
    KeyProblem.NOT_ED25519: (
        "Нужен ключ ssh-ed25519, он начинается с «ssh-ed25519 AAAA». "
        "Создайте его командой <code>ssh-keygen -t ed25519</code>."
    ),
    KeyProblem.BAD_FORMAT: (
        "Не удалось разобрать ключ. Пришлите содержимое файла .pub одной строкой, "
        "например «ssh-ed25519 AAAA… comment»."
    ),
}


def key_accepted(fingerprint: str) -> str:
    return (
        f"Ключ принят, отпечаток <code>{esc(fingerprint)}</code>. "
        "Выдаю доступ, это занимает меньше минуты."
    )


def key_replaced(fingerprint: str) -> str:
    return (
        f"Ключ принят, отпечаток <code>{esc(fingerprint)}</code>. "
        "Замена применится в течение минуты, прежний ключ перестанет работать."
    )


KEY_UNCHANGED = "Этот ключ уже установлен."
KEY_NO_CLIENT = "Сначала получите доступ: тариф или инвайт беты — /start."
KEY_BLOCKED = (
    "Хранилище заблокировано, ключ сейчас не заменить. Продлите доступ в разделе «Тарифы»."
)
NOT_UNDERSTOOD = "Не понял. Список команд — /help."
THROTTLED = "Слишком часто. Подождите несколько секунд."
NO_STORAGE = "У вас пока нет хранилища. Начните с /start."


def access_ready(client: Client, node: Node) -> str:
    host, login = esc(node.public_host), esc(client.login)
    return (
        "Доступ готов.\n\n"
        f"Хост: <code>{host}</code>\n"
        f"Логин: <code>{login}</code>\n"
        f"Лимит: {gb(client.quota_gb)}\n\n"
        "Добавьте в <code>~/.ssh/config</code>:\n"
        f"<pre>Host {SSH_ALIAS}\n    HostName {host}\n    User {login}\n"
        "    IdentityFile ~/.ssh/plugbackup\n    IdentitiesOnly yes</pre>\n"
        "Создайте репозиторий restic и сделайте первую копию:\n"
        f"<pre>restic -r sftp:{SSH_ALIAS}:{REPO_PATH} init\n"
        f"restic -r sftp:{SSH_ALIAS}:{REPO_PATH} backup ~/Documents</pre>\n"
        "Для rclone создайте SFTP-хранилище с тем же хостом, логином и ключом "
        "и включите шифрование имён файлов (crypt).\n\n"
        "Пароль репозитория придумайте и сохраните сами. У нас его нет: если вы его потеряете, "
        "восстановить данные будет нельзя."
    )


KEY_CHANGED_NOTICE = (
    "SSH-ключ вашего хранилища заменён. Если это сделали не вы, срочно напишите администратору."
)
ACCESS_DELAYED = (
    "Доступ ещё не выдан: сервер хранения временно недоступен. "
    "Мы повторяем попытку, администратор уже уведомлён."
)
ACCESS_FAILED = (
    "Не удалось выдать доступ автоматически. Администратор уже уведомлён и свяжется с вами."
)

# --- тариф и оплата ---


def plan_offer(plan: Plan, *, replaces: bool) -> str:
    text = (
        f"Тариф {esc(plan.name)}: {plan.price_rub}{NBSP}₽/мес, {plan.stars}{NBSP}Stars. "
        "Подписка на 30 дней продлевается автоматически, продление можно отключить в любой момент."
    )
    if replaces:
        text += (
            "\n\nНовая подписка заменит текущую. Пересчёта за неиспользованные дни нет, "
            "прежняя подписка больше не продлится."
        )
    return text


def pay_button(plan: Plan) -> str:
    return f"Оплатить {plan.stars}{NBSP}Stars"


OFFER_UNAVAILABLE = "Не удалось проверить занятое место. Попробуйте ещё раз через минуту."
TRY_LATER = "Не удалось проверить оплату. Попробуйте ещё раз через минуту."

REJECTIONS: dict[Reject, str] = {
    Reject.BAD_PLAN: "Тариф не найден. Откройте «Тарифы» ещё раз.",
    Reject.PRICE_CHANGED: "Цена изменилась. Откройте «Тарифы» ещё раз.",
    Reject.NO_RISK_ACK: "Сначала подтвердите, что понимаете риски беты.",
    Reject.HAS_CLIENT: "У вас уже есть хранилище. Сменить тариф можно командой /upgrade.",
    Reject.NO_CLIENT: "Хранилище не найдено. Откройте «Тарифы» ещё раз.",
    Reject.ALREADY_SUBSCRIBED: "Подписка на этот тариф уже оформлена.",
    Reject.NO_CAPACITY: "Свободных мест на этом тарифе сейчас нет. Попробуйте позже.",
    Reject.TOO_MUCH_DATA: (
        "Занято больше, чем помещается в новый тариф. Удалите лишнее или выберите тариф побольше."
    ),
    Reject.ADMIN_HOLD: "Доступ ограничен администратором, оплата сейчас невозможна.",
}

PAYMENT_MANUAL = (
    "Оплата получена, но выдать доступ автоматически не удалось. "
    "Администратор уже уведомлён и свяжется с вами."
)


def paid_new(plan: Plan, until: str) -> str:
    return f"Оплата получена. Тариф {esc(plan.name)}, действует до {until}.\n\n{KEY_HELP}"


def plan_changed(plan: Plan, until: str, *, resumed: bool, ask_key: bool) -> str:
    text = f"Тариф изменён на {esc(plan.name)}, действует до {until}."
    if resumed:
        text += " Доступ возобновлён."
    elif not ask_key:
        text += " Новый лимит применится в течение нескольких минут."
    if ask_key:
        text += f"\n\n{KEY_HELP}"
    return text


def renewed(until: str, *, resumed: bool) -> str:
    text = f"Продление принято, доступ действует до {until}."
    if resumed:
        text += " Доступ возобновлён."
    return text


def renewal_canceled(until: str) -> str:
    return (
        f"Автопродление отключено. Доступ сохранится до {until}. "
        "Включить его снова можно в настройках подписок Telegram или в /status."
    )


RENEWAL_ENABLED = "Автопродление включено."
RENEWAL_CHANGE_FAILED = "Не удалось изменить продление. Попробуйте позже."
BTN_RENEWAL_OFF = "Отключить продление"
BTN_RENEWAL_ON = "Включить продление"


def renewal_failed(until: str) -> str:
    return (
        "Не удалось списать оплату за продление. "
        f"Доступ сохранится до {until}, затем хранилище будет заблокировано. "
        "Оформить подписку заново можно в разделе «Тарифы»."
    )


# --- статус ---

_STATE_NAMES = {
    State.PENDING: "ожидает ключ",
    State.ACTIVE: "активно",
    State.EXPIRING: "скоро закончится",
    State.SUSPENDED: "заблокировано",
    State.DELETED: "удалено",
}


def status(view: StatusView, tz: str) -> str:
    client = view.client
    name = view.plan.name if view.plan else client.plan_id
    lines = [f"Тариф: {esc(name)}", f"Состояние: {_STATE_NAMES[client.state]}"]

    quota_bytes = client.quota_gb * GIB
    if view.sample is not None:
        percent = round(view.sample.used_bytes * 100 / quota_bytes)
        lines.append(
            f"Занято: {fmt_bytes(view.sample.used_bytes)} из {gb(client.quota_gb)} ({percent}%), "
            f"данные на {fmt_datetime(view.sample.hour_ts, tz)}"
        )
    else:
        lines.append(f"Лимит: {gb(client.quota_gb)}, данных о занятом месте пока нет")

    if client.confirm_due_at is not None:
        lines.append(f"Следующий вопрос «Я ещё пользуюсь»: {fmt_date(client.confirm_due_at, tz)}")
    elif client.expires_at is not None:
        date = fmt_date(client.expires_at, tz)
        if client.renewal_on:
            lines.append(f"Следующее списание: {date}")
        else:
            lines.append(f"Доступ до {date}, продление отключено")

    if view.node is not None:
        lines.append(
            f"Хост: <code>{esc(view.node.public_host)}</code>, "
            f"логин: <code>{esc(client.login)}</code>"
        )
    return "\n".join(lines)


BTN_ALIVE = "Я ещё пользуюсь"
ALIVE_THANKS = "Спасибо. Следующий вопрос — {date}."
ALIVE_RESUMED = "Спасибо. Доступ возобновлён, следующий вопрос — {date}."
ALIVE_NO_CLIENT = "Хранилище не найдено. Начать заново можно через /start."
ALIVE_BLOCKED = "Доступ ограничен администратором, кнопка сейчас не поможет."

# --- восстановление ---

RESTORE_NO_CLIENT = "Восстанавливать пока нечего: у вас нет работающего хранилища."
RESTORE_BLOCKED = "Хранилище заблокировано. Сначала верните доступ в разделе «Тарифы»."
RESTORE_BAD_DATE = "Такого снимка нет в списке. Откройте /restore ещё раз."
RESTORE_OPEN = "Ваша заявка уже у администратора. Дождитесь ответа, новую можно подать после него."
RESTORE_TOO_MANY = "Сегодня вы уже подавали несколько заявок. Новую можно будет подать завтра."
RESTORE_CANCELLED = "Заявка отменена."
BTN_RESTORE_SEND = "Отправить заявку"
BTN_CANCEL = "Отмена"

RESTORE_INTRO = (
    "Из какого снимка восстановить данные? Снимки делаются раз в сутки и хранятся 7 дней.\n\n"
    "Автоматического отката нет: заявку рассматривает администратор, текущие данные до "
    "согласования с вами не изменятся."
)


def restore_confirm(date: str) -> str:
    return (
        f"Заявка на восстановление из снимка за {date}. Администратор проверит, что снимок "
        "есть, и свяжется с вами."
    )


def restore_accepted(request_id: int) -> str:
    return f"Заявка №{request_id} принята. Администратор свяжется с вами в этом чате."


def restore_resolved(request_id: int, date: str, *, done: bool) -> str:
    if done:
        return f"Заявка №{request_id} на восстановление из снимка за {date} выполнена."
    return f"Заявка №{request_id} на восстановление из снимка за {date} отклонена."
