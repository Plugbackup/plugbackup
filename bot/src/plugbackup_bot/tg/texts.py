"""Сборка клиентских сообщений: карточки из шаблонов texts_ru.

Сами формулировки лежат в texts_ru.py. Здесь только подстановка данных и порядок блоков.
Сообщения уходят с parse_mode=HTML; подставляемое экранируется (ui.markup.fill, ui.cards).
Тексты для pre_checkout (`REJECTIONS`) показываются Telegram как обычный текст.
"""

from collections.abc import Sequence

from plugbackup_bot import texts_ru as T
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
from plugbackup_bot.emoji import E
from plugbackup_bot.services.access import RedeemKind, StatusView
from plugbackup_bot.services.billing import Reject
from plugbackup_bot.tg.format import fmt_bytes, fmt_date, fmt_datetime, gb, quota_bar
from plugbackup_bot.ui import cards
from plugbackup_bot.ui.markup import Raw, esc, fill

__all__ = ["esc"]

# Путь репозитория на ноде внутри chroot клиента.
REPO_PATH = "/data/restic"
SSH_ALIAS = "plugbackup"
RISK_TEXT_VERSION = T.RISK_TEXT_VERSION


def _days(seconds: int) -> int:
    return seconds // (24 * 3600)


# --- главное меню и справка ---


def start(has_client: bool, beta_gb: int) -> str:
    return cards.card(
        T.START_TITLE,
        T.START_BODY,
        fill(T.START_BETA, beta_gb=beta_gb),
        T.START_HAS_CLIENT if has_client else None,
        icon=E.disk,
    )


def how_it_works() -> str:
    return cards.card(T.HOW_TITLE, T.HOW_STEPS, cards.quote(T.HOW_DETAILS))


def help_text() -> str:
    return cards.card(T.HELP_TITLE, T.HELP_COMMANDS)


def rules() -> str:
    bullets = [
        fill(
            bullet,
            beta_confirm_days=_days(BETA_CONFIRM_PERIOD_S),
            beta_grace_days=_days(BETA_GRACE_S),
            paid_grace_days=_days(PAID_GRACE_S),
            suspended_days=_days(SUSPENDED_TTL_S),
        )
        for bullet in T.RULES_BULLETS
    ]
    return cards.card(T.RULES_TITLE, cards.bullets(bullets))


HOW_IT_WORKS = how_it_works()
HELP = help_text()


# --- тарифы ---


def tariffs(plans: Sequence[Plan], current: Client | None) -> str:
    lines = [
        fill(T.TARIFF_LINE, name=plan.name, price_rub=plan.price_rub, stars=plan.stars)
        for plan in plans
    ]
    return cards.card(
        T.TARIFFS_TITLE,
        cards.bullets(lines) if lines else T.TARIFFS_NONE,
        cards.quote(T.TARIFFS_NOTE),
        fill(T.TARIFFS_CURRENT, quota=gb(current.quota_gb)) if current is not None else None,
        icon=E.payment,
    )


def plan_offer(plan: Plan, *, replaces: bool) -> str:
    return cards.card(
        T.OFFER_TITLE.format(name=plan.name),
        fill(T.OFFER_LINE, price_rub=plan.price_rub, stars=plan.stars),
        T.OFFER_NOTE,
        T.OFFER_REPLACES if replaces else None,
        icon=E.payment,
    )


def pay_button(plan: Plan) -> str:
    return T.BTN_PAY.format(stars=plan.stars)


# --- риски беты ---


def risk_text() -> str:
    return cards.card(
        T.RISK_TITLE,
        T.RISK_INTRO,
        cards.bullets(T.RISK_BULLETS),
        T.RISK_OUTRO,
        icon=E.warning,
    )


RISK_TEXT = risk_text()


# --- инвайт беты ---

INVITE_ERRORS: dict[RedeemKind, str] = {
    RedeemKind.INVALID: T.INVITE_INVALID,
    RedeemKind.USED: T.INVITE_USED,
    RedeemKind.EXPIRED: T.INVITE_EXPIRED,
    RedeemKind.ALREADY_BETA: T.INVITE_ALREADY_BETA,
    RedeemKind.HAS_CLIENT: T.INVITE_HAS_CLIENT,
    RedeemKind.NO_CAPACITY: T.INVITE_NO_CAPACITY,
    RedeemKind.NEED_ACK: T.INVITE_NEED_ACK,
}


def beta_ask_code() -> str:
    return cards.card(T.BETA_ASK_TITLE, T.BETA_ASK_CODE, icon=E.invite)


def beta_accepted(plan: Plan) -> str:
    return cards.card(
        T.BETA_ACCEPTED_TITLE,
        fill(T.BETA_ACCEPTED_LINE, quota=gb(plan.gb)),
        T.KEY_ASK,
        cards.quote(T.KEY_HOWTO),
        icon=E.ok,
    )


# --- ключ ---


def key_help(*, replace: bool = False) -> str:
    return cards.card(
        T.KEY_TITLE,
        T.KEY_ASK_REPLACE if replace else T.KEY_ASK,
        cards.quote(T.KEY_HOWTO),
        icon=E.key,
    )


def pending_key_prompt() -> str:
    return cards.card(
        T.KEY_TITLE, T.PENDING_KEY_PROMPT, T.KEY_ASK, cards.quote(T.KEY_HOWTO), icon=E.key
    )


KEY_HELP = key_help()
KEY_REPLACE_HELP = key_help(replace=True)

KEY_PROBLEMS: dict[KeyProblem, str] = {
    KeyProblem.EMPTY: T.KEY_PROBLEM_EMPTY,
    KeyProblem.TOO_LONG: T.KEY_PROBLEM_TOO_LONG,
    KeyProblem.PRIVATE_KEY: T.KEY_PROBLEM_PRIVATE,
    KeyProblem.NOT_ED25519: T.KEY_PROBLEM_NOT_ED25519,
    KeyProblem.BAD_FORMAT: T.KEY_PROBLEM_BAD_FORMAT,
}


def key_accepted(fingerprint: str) -> str:
    return cards.card(
        T.KEY_ACCEPTED_TITLE,
        cards.field(T.KEY_ACCEPTED_FINGERPRINT, fingerprint),
        T.KEY_ACCEPTED_NEW,
        icon=E.ok,
    )


def key_replaced(fingerprint: str) -> str:
    return cards.card(
        T.KEY_ACCEPTED_TITLE,
        cards.field(T.KEY_ACCEPTED_FINGERPRINT, fingerprint),
        T.KEY_ACCEPTED_REPLACED,
        icon=E.ok,
    )


# --- доступ готов ---


def restic_commands() -> tuple[str, str]:
    """Команды restic для нового репозитория: создать и сделать первую копию."""
    target = f"sftp:{SSH_ALIAS}:{REPO_PATH}"
    return f"restic -r {target} init", f"restic -r {target} backup ~/Documents"


def access_ready(client: Client, node: Node) -> str:
    init, backup = restic_commands()
    config = (
        f"<pre>Host {SSH_ALIAS}\n    HostName {esc(node.public_host)}\n"
        f"    User {esc(client.login)}\n    IdentityFile ~/.ssh/plugbackup\n"
        "    IdentitiesOnly yes</pre>"
    )
    return cards.card(
        T.ACCESS_READY_TITLE,
        cards.lines(
            cards.field(T.ACCESS_HOST, node.public_host),
            cards.field(T.ACCESS_LOGIN, client.login),
            cards.plain_field(T.ACCESS_QUOTA, gb(client.quota_gb)),
        ),
        f"{T.ACCESS_SSH_CONFIG}\n{config}",
        cards.quote(fill(T.ACCESS_DETAILS, restic_init=init, restic_backup=backup)),
        icon=E.ok,
    )


# --- оплата ---

REJECTIONS: dict[Reject, str] = {
    Reject.BAD_PLAN: T.REJECT_BAD_PLAN,
    Reject.PRICE_CHANGED: T.REJECT_PRICE_CHANGED,
    Reject.NO_RISK_ACK: T.REJECT_NO_RISK_ACK,
    Reject.HAS_CLIENT: T.REJECT_HAS_CLIENT,
    Reject.NO_CLIENT: T.REJECT_NO_CLIENT,
    Reject.ALREADY_SUBSCRIBED: T.REJECT_ALREADY_SUBSCRIBED,
    Reject.NO_CAPACITY: T.REJECT_NO_CAPACITY,
    Reject.TOO_MUCH_DATA: T.REJECT_TOO_MUCH_DATA,
    Reject.ADMIN_HOLD: T.REJECT_ADMIN_HOLD,
}


def paid_new(plan: Plan, until: str) -> str:
    return cards.card(
        T.PAID_NEW_TITLE,
        cards.lines(
            cards.plain_field(T.PAID_NEW_PLAN, plan.name), cards.plain_field(T.PAID_UNTIL, until)
        ),
        T.PAID_NEXT_STEP,
        icon=E.ok,
    )


def plan_changed(plan: Plan, until: str, *, resumed: bool, ask_key: bool) -> str:
    note = None
    if resumed:
        note = T.PLAN_CHANGED_RESUMED
    elif not ask_key:
        note = T.PLAN_CHANGED_NEW_LIMIT
    return cards.card(
        T.PLAN_CHANGED_TITLE,
        cards.lines(
            cards.plain_field(T.PAID_NEW_PLAN, plan.name), cards.plain_field(T.PAID_UNTIL, until)
        ),
        note,
        T.PAID_NEXT_STEP if ask_key else None,
        icon=E.ok,
    )


def renewed(until: str, *, resumed: bool) -> str:
    return cards.card(
        T.RENEWED_TITLE,
        fill(T.RENEWED_UNTIL, until=until),
        T.RENEWED_RESUMED if resumed else None,
        icon=E.ok,
    )


def renewal_canceled(until: str) -> str:
    return fill(T.RENEWAL_CANCELED, until=until)


def renewal_failed(until: str) -> str:
    return fill(T.RENEWAL_FAILED, until=until)


# --- статус ---

_STATE_NAMES = {
    State.PENDING: T.STATE_PENDING,
    State.ACTIVE: T.STATE_ACTIVE,
    State.EXPIRING: T.STATE_EXPIRING,
    State.SUSPENDED: T.STATE_SUSPENDED,
    State.DELETED: T.STATE_DELETED,
}


def status(view: StatusView, tz: str) -> str:
    client = view.client
    name = view.plan.name if view.plan else client.plan_id
    rows = [
        cards.plain_field(T.STATUS_PLAN, name),
        cards.plain_field(T.STATUS_STATE, _STATE_NAMES[client.state]),
    ]

    if view.sample is not None:
        bar = quota_bar(view.sample.used_bytes, client.quota_gb * GIB)
        rows.append(cards.field(T.STATUS_USED, bar))
        rows.append(
            fill(
                T.STATUS_USED_DETAIL,
                used=fmt_bytes(view.sample.used_bytes),
                quota=gb(client.quota_gb),
                at=fmt_datetime(view.sample.hour_ts, tz),
            )
        )
    else:
        rows.append(fill(T.STATUS_NO_USAGE, quota=gb(client.quota_gb)))

    if client.confirm_due_at is not None:
        rows.append(cards.plain_field(T.STATUS_NEXT_QUESTION, fmt_date(client.confirm_due_at, tz)))
    elif client.expires_at is not None:
        date = fmt_date(client.expires_at, tz)
        if client.renewal_on:
            rows.append(cards.plain_field(T.STATUS_NEXT_CHARGE, date))
        else:
            rows.append(
                cards.plain_field(T.STATUS_ACCESS_UNTIL, fill(T.STATUS_RENEWAL_OFF, date=date))
            )

    if view.node is not None:
        rows.append(cards.field(T.STATUS_HOST, view.node.public_host))
        rows.append(cards.field(T.STATUS_LOGIN, client.login))
    needs_key = client.state is State.PENDING and client.ssh_key_body is None
    return cards.card(
        T.STATUS_TITLE,
        cards.lines(*rows),
        fill(T.STATUS_NEEDS_KEY, prompt=T.PENDING_KEY_PROMPT) if needs_key else None,
        icon=E.chart,
    )


# --- восстановление ---


def restore_intro() -> str:
    return cards.card(
        T.RESTORE_TITLE, T.RESTORE_INTRO, cards.quote(T.RESTORE_INTRO_DETAILS), icon=E.refresh
    )


def restore_confirm(date: str) -> str:
    return cards.card(T.RESTORE_CONFIRM_TITLE, fill(T.RESTORE_CONFIRM, date=date), icon=E.refresh)


def restore_accepted(request_id: int) -> str:
    return fill(T.RESTORE_ACCEPTED, request_id=request_id)


def restore_resolved(request_id: int, date: str, *, done: bool, note: str | None = None) -> str:
    template = T.RESTORE_RESOLVED_DONE if done else T.RESTORE_RESOLVED_REJECTED
    text = fill(template, request_id=request_id, date=date)
    if not note:
        return text
    return Raw(f"{text}\n\n{fill(T.RESTORE_RESOLVED_NOTE, note=note)}")
