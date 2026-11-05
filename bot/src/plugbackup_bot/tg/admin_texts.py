"""Тексты админки: статистика, карточка клиента, ноды."""

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from plugbackup_bot.domain.plans import PlanCatalog
from plugbackup_bot.domain.states import State
from plugbackup_bot.services.admin import ClientCard, NodeLoad
from plugbackup_bot.services.stats import Stats
from plugbackup_bot.tg.format import NBSP, fmt_bytes, fmt_date, fmt_datetime, gb
from plugbackup_bot.tg.texts import esc

STATE_NAMES = {
    State.PENDING: "ожидает ключ",
    State.ACTIVE: "активно",
    State.EXPIRING: "скоро закончится",
    State.SUSPENDED: "заблокировано",
    State.DELETED: "удалено",
}
ACTION_NAMES = {"s": "Заблокировать", "r": "Вернуть доступ", "d": "Удалить"}
ACTION_VERBS = {"s": "заблокировать", "r": "вернуть доступ", "d": "удалить"}

# Тексты клиенту о действиях администратора.
CLIENT_SUSPENDED = (
    "Хранилище заблокировано администратором. Если это ошибка, напишите администратору."
)
CLIENT_RESUMED = "Доступ к хранилищу возобновлён."
CLIENT_DELETED = "Хранилище удалено администратором."


def _plan_name(catalog: PlanCatalog, plan_id: str) -> str:
    plan = catalog.find(plan_id)
    return plan.name if plan else plan_id


def stats(data: Stats, catalog: PlanCatalog, tz: str) -> str:
    lines = ["Статистика", ""]
    lines.append("Клиенты по тарифам:")
    if data.by_plan:
        lines += [
            f"• {esc(_plan_name(catalog, plan_id))} — {count}"
            for plan_id, count in sorted(data.by_plan.items())
        ]
    else:
        lines.append("• пока нет")
    states = ", ".join(
        f"{STATE_NAMES[State(state)]} {count}" for state, count in sorted(data.by_state.items())
    )
    lines += [f"По состояниям: {states or 'нет клиентов'}", f"Платящих: {data.paying}", ""]

    lines.append("Ноды (продано / лимит):")
    for load in data.nodes:
        node = load.node
        status = "" if node.enabled else ", отключена"
        if node.down_since is not None:
            status += f", недоступна с {fmt_datetime(node.down_since, tz)}"
        lines.append(
            f"• {esc(node.name)}: {load.sold_gb} / {node.limit_gb}{NBSP}ГБ, "
            f"свободно {load.free_gb}{NBSP}ГБ{status}"
        )
    lines += [
        "",
        f"Выручка с {fmt_date(data.month_start, tz)}: {data.revenue_stars}{NBSP}Stars",
        f"Очередь storectl: ждёт {data.pending_jobs}, упало {data.failed_jobs}",
        f"Заявок на восстановление: {data.open_restores}",
        f"Неиспользованных инвайтов: {data.unused_invites}",
    ]
    return "\n".join(lines)


def nodes(loads: Sequence[NodeLoad], tz: str) -> str:
    lines = ["Ноды", ""]
    for load in loads:
        node = load.node
        lines.append(
            f"• {esc(node.name)} — {esc(node.addr)} → {esc(node.public_host)}\n"
            f"  ёмкость {node.capacity_gb}{NBSP}ГБ, коэффициент {node.oversell:g}, "
            f"лимит {node.limit_gb}{NBSP}ГБ, продано {load.sold_gb}{NBSP}ГБ"
            + ("" if node.enabled else ", отключена")
            + (f", недоступна с {fmt_datetime(node.down_since, tz)}" if node.down_since else "")
        )
    lines += ["", "Список нод задаётся в .env (NODES) и применяется при перезапуске бота."]
    return "\n".join(lines)


def invites(codes: Sequence[str]) -> str:
    body = "\n".join(f"<code>{esc(code)}</code>" for code in codes)
    return f"Инвайт-коды ({len(codes)}), каждый действует один раз:\n\n{body}"


def client_card(card: ClientCard, catalog: PlanCatalog, tz: str) -> str:
    client = card.client
    lines = [
        f"Клиент <code>{esc(client.login)}</code>",
        f"tg_id: <code>{client.tg_id}</code>",
        f"Тариф: {esc(_plan_name(catalog, client.plan_id))}, лимит {gb(client.quota_gb)}",
        f"Состояние: {STATE_NAMES[client.state]} (с {fmt_datetime(client.state_since, tz)})",
        f"Нода: {esc(card.node.name) if card.node else 'не найдена'}",
    ]
    if card.sample is not None:
        lines.append(
            f"Занято: {fmt_bytes(card.sample.used_bytes)}"
            f" (данные на {fmt_datetime(card.sample.hour_ts, tz)})"
        )
    if client.expires_at is not None:
        lines.append(f"Оплачено до: {fmt_date(client.expires_at, tz)}")
    if client.confirm_due_at is not None:
        lines.append(f"Следующий вопрос «Я ещё пользуюсь»: {fmt_date(client.confirm_due_at, tz)}")
    if client.sub_charge_id is not None:
        lines.append(f"Продление: {'включено' if client.renewal_on else 'выключено'}")
    lines.append(f"Ручная блокировка: {'да' if client.admin_hold else 'нет'}")
    lines.append(f"Ключ: {'есть' if client.ssh_key_body else 'не прислан'}")
    if card.payments:
        lines += ["", "Платежи:"]
        for payment in card.payments:
            kind = "продление" if payment.recurring else "оплата"
            refunded = ", возврат" if payment.status == "refunded" else ""
            lines.append(
                f"• {payment.stars}{NBSP}Stars, {fmt_date(payment.at, tz)}, "
                f"{esc(_plan_name(catalog, payment.plan_id))} ({kind}{refunded})"
            )
    if card.open_jobs:
        lines += ["", "Задачи storectl:"]
        for job in card.open_jobs:
            error = f": {esc(job.last_error)}" if job.last_error else ""
            status = "упала" if job.status.value == "failed" else "ждёт повтора"
            lines.append(f"• #{job.id} {job.op.value} — {status}, попыток {job.attempts}{error}")
    return "\n".join(lines)


def client_buttons(card: ClientCard) -> InlineKeyboardMarkup:
    client = card.client
    rows: list[list[InlineKeyboardButton]] = []

    def button(action: str) -> list[InlineKeyboardButton]:
        return [
            InlineKeyboardButton(
                text=ACTION_NAMES[action], callback_data=f"ac:ask:{action}:{client.id}"
            )
        ]

    if client.state in (State.ACTIVE, State.EXPIRING):
        rows.append(button("s"))
    if client.state is State.SUSPENDED:
        rows.append(button("r"))
    if client.state is not State.DELETED:
        rows.append(button("d"))
    for job in card.open_jobs:
        if job.status.value == "failed":
            rows.append(
                [InlineKeyboardButton(text=f"Повторить #{job.id}", callback_data=f"ac:rq:{job.id}")]
            )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_buttons(action: str, client_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Подтвердить", callback_data=f"ac:do:{action}:{client_id}"
                ),
                InlineKeyboardButton(text="Отмена", callback_data="ac:no"),
            ]
        ]
    )


ADMIN_HELP = (
    "Админка\n\n"
    "/stats — клиенты по тарифам, проданное место, выручка\n"
    "/nodes — ноды и их загрузка\n"
    "/invite N — инвайт-коды беты, от 1 до 50\n"
    "/client u1001 — карточка клиента по логину или Telegram id, там блокировка и удаление\n"
    "/broadcast — рассылка всем пользователям\n"
    "/maint нода текст — сообщение клиентам о плановых работах, вместо ноды можно all\n"
    "/maint_end N — завершить плановые работы\n\n"
    "Кнопки ниже запускают самое частое."
)

ADMIN_STATS = "adm:stats"
ADMIN_NODES = "adm:nodes"
ADMIN_INVITE = "adm:invite"
ADMIN_BROADCAST = "adm:broadcast"


def admin_buttons() -> InlineKeyboardMarkup:
    def button(text: str, data: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=data)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [button("Статистика", ADMIN_STATS), button("Ноды", ADMIN_NODES)],
            [button("Инвайт-код", ADMIN_INVITE), button("Рассылка", ADMIN_BROADCAST)],
        ]
    )
