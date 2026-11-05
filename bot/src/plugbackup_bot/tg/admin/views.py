"""Тексты и клавиатуры админки: дашборд, список и карточка клиента."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from plugbackup_bot import texts_ru as T
from plugbackup_bot.db.clients import Client
from plugbackup_bot.domain.alerts import BY_KEY
from plugbackup_bot.domain.plans import GIB, PlanCatalog
from plugbackup_bot.domain.roles import Actor, Permission
from plugbackup_bot.domain.states import State
from plugbackup_bot.emoji import E
from plugbackup_bot.services.admin import ClientCard, ClientRow, NodeLoad
from plugbackup_bot.services.stats import Stats
from plugbackup_bot.tg.admin.core import CLIENTS, admin_nav, cb
from plugbackup_bot.tg.format import fmt_bytes, fmt_date, fmt_datetime, gb, quota_bar
from plugbackup_bot.ui import buttons, cards
from plugbackup_bot.ui.markup import esc, fill

STATE_NAMES = {
    State.PENDING: T.STATE_PENDING,
    State.ACTIVE: T.STATE_ACTIVE,
    State.EXPIRING: T.STATE_EXPIRING,
    State.SUSPENDED: T.STATE_SUSPENDED,
    State.DELETED: T.STATE_DELETED,
}


def plan_name(catalog: PlanCatalog, plan_id: str) -> str:
    plan = catalog.find(plan_id)
    return plan.name if plan else plan_id


# --- дашборд ---


def _node_line(load: NodeLoad, tz: str) -> str:
    node = load.node
    values = {
        "name": node.name,
        "sold": load.sold_gb,
        "limit": node.limit_gb,
    }
    if not node.enabled:
        return fill(T.DASH_NODE_OFF, **values)
    if getattr(node, "maintenance", False):
        return fill(T.DASH_NODE_MAINT, **values)
    if node.down_since is not None:
        return fill(T.DASH_NODE_DOWN, since=fmt_datetime(node.down_since, tz), **values)
    return fill(T.DASH_NODE_UP, **values)


def dashboard(data: Stats, catalog: PlanCatalog, tz: str, open_tickets: int = 0) -> str:
    if data.by_plan:
        plans = cards.bullets(
            fill("{name} — {count}", name=plan_name(catalog, plan_id), count=count)
            for plan_id, count in sorted(data.by_plan.items())
        )
    else:
        plans = cards.bullets([T.DASH_NO_CLIENTS])
    states = ", ".join(
        T.STATE_LINE.format(name=STATE_NAMES[State(state)], count=count)
        for state, count in sorted(data.by_state.items())
    )
    sold = sum(load.sold_gb for load in data.nodes)
    limit = sum(load.node.limit_gb for load in data.nodes)
    percent = round(sold * 100 / limit) if limit else 0
    clients = cards.lines(
        f"<b>{T.DASH_CLIENTS}</b>",
        plans,
        fill(T.DASH_STATES, states=states or "—"),
        fill(T.DASH_PAYING, count=data.paying),
        fill(T.DASH_NEW_WEEK, count=data.new_week),
    )
    money = cards.lines(
        fill(T.DASH_REVENUE, since=fmt_date(data.month_start, tz), stars=data.revenue_stars),
        fill(T.DASH_SOLD, sold=sold, limit=limit, percent=percent),
    )
    nodes = cards.lines(
        f"<b>{T.DASH_NODES}</b>", cards.bullets(_node_line(load, tz) for load in data.nodes)
    )
    queue = cards.lines(
        f"<b>{T.DASH_QUEUE}</b>",
        fill(T.DASH_JOBS, pending=data.pending_jobs, failed=data.failed_jobs),
        fill(T.DASH_RESTORES, count=data.open_restores),
        fill(T.DASH_TICKETS, count=open_tickets),
        fill(T.DASH_INVITES, count=data.unused_invites),
    )
    return cards.card(T.DASH_TITLE, clients, money, nodes, queue, icon=E.chart)


# --- клиенты ---


def client_row_label(row: ClientRow, catalog: PlanCatalog) -> str:
    template = T.CLIENT_ROW_USERNAME if row.username else T.CLIENT_ROW
    return template.format(
        login=row.login,
        username=row.username or "",
        plan=plan_name(catalog, row.plan_id),
        state=STATE_NAMES[row.state],
    )


def notice_title(kind: str) -> str:
    """Читаемое название уведомления из таблицы notifications."""
    if kind.startswith("alert:") and kind.removeprefix("alert:") in BY_KEY:
        return BY_KEY[kind.removeprefix("alert:")].title
    if kind in T.NOTICES:
        return T.NOTICES[kind][0]
    if kind == "service":
        return T.SERVICE_DEFAULT_TITLE
    return kind


def client_card(card: ClientCard, catalog: PlanCatalog, tz: str) -> str:
    client = card.client
    rows = [
        cards.field(T.CARD_TG_ID, client.tg_id),
        cards.plain_field(T.CARD_USERNAME, f"@{card.username}") if card.username else None,
        cards.plain_field(
            T.CARD_PLAN,
            fill(
                T.CARD_PLAN_VALUE,
                name=plan_name(catalog, client.plan_id),
                quota=gb(client.quota_gb),
            ),
        ),
        cards.plain_field(T.CARD_BONUS, fill(T.CARD_BONUS_VALUE, gb=client.bonus_gb))
        if client.bonus_gb
        else None,
        cards.plain_field(
            T.CARD_STATE,
            fill(
                T.CARD_STATE_VALUE,
                state=STATE_NAMES[client.state],
                since=fmt_datetime(client.state_since, tz),
            ),
        ),
        cards.plain_field(T.CARD_NODE, card.node.name if card.node else T.CARD_NODE_MISSING),
    ]
    if card.sample is not None:
        bar = quota_bar(card.sample.used_bytes, client.quota_gb * GIB)
        rows.append(cards.field(T.CARD_USED, bar))
        rows.append(
            fill(
                T.CARD_USED_DETAIL,
                used=fmt_bytes(card.sample.used_bytes),
                at=fmt_datetime(card.sample.hour_ts, tz),
            )
        )
    if client.expires_at is not None:
        rows.append(cards.plain_field(T.CARD_PAID_UNTIL, fmt_date(client.expires_at, tz)))
    if client.confirm_due_at is not None:
        rows.append(cards.plain_field(T.CARD_NEXT_QUESTION, fmt_date(client.confirm_due_at, tz)))
    if client.sub_charge_id is not None:
        rows.append(
            cards.plain_field(T.CARD_RENEWAL, T.CARD_ON if client.renewal_on else T.CARD_OFF)
        )
    rows.append(cards.plain_field(T.CARD_HOLD, T.CARD_YES if client.admin_hold else T.CARD_NO))
    rows.append(
        cards.plain_field(T.CARD_KEY, T.CARD_KEY_HAS if client.ssh_key_body else T.CARD_KEY_NONE)
    )

    blocks = [cards.lines(*rows)]
    if client.admin_note:
        blocks.append(cards.lines(f"<b>{T.CARD_NOTE}</b>", cards.quote(esc(client.admin_note))))
    if card.payments:
        lines = []
        for payment in card.payments:
            kind = T.CARD_KIND_RENEWAL if payment.recurring else T.CARD_KIND_FIRST
            lines.append(
                fill(
                    T.CARD_PAYMENT_LINE,
                    stars=payment.stars,
                    date=fmt_date(payment.at, tz),
                    plan=plan_name(catalog, payment.plan_id),
                    kind=kind,
                    refunded=T.CARD_REFUNDED if payment.status == "refunded" else "",
                )
            )
        blocks.append(cards.lines(f"<b>{T.CARD_PAYMENTS}</b>", cards.bullets(lines)))
    if card.notices:
        lines = [
            fill(T.CARD_NOTICE_LINE, title=notice_title(kind), at=fmt_datetime(at, tz))
            for kind, at in card.notices
        ]
        blocks.append(cards.quote(cards.lines(f"<b>{T.CARD_NOTICES}</b>", cards.bullets(lines))))
    if card.open_jobs:
        lines = []
        for job in card.open_jobs:
            template = T.CARD_JOB_FAILED if job.status.value == "failed" else T.CARD_JOB_RETRY
            error = f": {job.last_error}" if job.last_error else ""
            lines.append(
                fill(template, id=job.id, op=job.op.value, attempts=job.attempts, error=error)
            )
        blocks.append(cards.lines(f"<b>{T.CARD_JOBS}</b>", cards.bullets(lines)))
    return cards.card(T.CARD_TITLE.format(login=client.login), *blocks, icon=E.user)


def client_markup(card: ClientCard, actor: Actor, catalog: PlanCatalog) -> InlineKeyboardMarkup:
    client: Client = card.client
    rows: list[list[InlineKeyboardButton]] = []
    live = client.state in (State.ACTIVE, State.EXPIRING, State.SUSPENDED)
    manage: list[InlineKeyboardButton] = []
    if live and actor.can(Permission.CLIENTS_EXTEND):
        manage.append(buttons.plain(T.BTN_CLIENT_EXTEND, cb(CLIENTS, "ex", i=client.id), E.clock))
    if live and actor.can(Permission.CLIENTS_PLAN) and client.plan_id != catalog.beta.id:
        manage.append(buttons.plain(T.BTN_CLIENT_PLAN, cb(CLIENTS, "pl", i=client.id), E.payment))
    if live and actor.can(Permission.CLIENTS_BONUS):
        manage.append(buttons.plain(T.BTN_CLIENT_BONUS, cb(CLIENTS, "bn", i=client.id), E.star))
    rows.extend([button] for button in manage)

    contact: list[InlineKeyboardButton] = []
    if actor.can(Permission.CLIENTS_NOTE):
        contact.append(buttons.plain(T.BTN_CLIENT_NOTE, cb(CLIENTS, "nt", i=client.id), E.settings))
    if actor.can(Permission.CLIENTS_MESSAGE) and client.state is not State.DELETED:
        contact.append(
            buttons.plain(T.BTN_CLIENT_MESSAGE, cb(CLIENTS, "ms", i=client.id), E.support)
        )
    if contact:
        rows.append(contact)

    if actor.can(Permission.CLIENTS_BLOCK):
        if client.state in (State.ACTIVE, State.EXPIRING):
            rows.append(
                [buttons.danger(T.BTN_CLIENT_BLOCK, cb(CLIENTS, "bk", i=client.id), E.lock)]
            )
        elif client.state is State.SUSPENDED:
            rows.append(
                [buttons.success(T.BTN_CLIENT_UNBLOCK, cb(CLIENTS, "ub", i=client.id), E.ok)]
            )
    if actor.can(Permission.CLIENTS_DELETE) and client.state is not State.DELETED:
        rows.append([buttons.danger(T.BTN_CLIENT_DELETE, cb(CLIENTS, "dl", i=client.id), E.trash)])
    if actor.can(Permission.CLIENTS_VIEW):
        for job in card.open_jobs:
            if job.status.value == "failed":
                rows.append(
                    [
                        buttons.plain(
                            T.BTN_JOB_RETRY.format(id=job.id),
                            cb(CLIENTS, "rq", i=job.id),
                            E.refresh,
                        )
                    ]
                )
    return buttons.markup(*rows, admin_nav(cb(CLIENTS, "list")))
