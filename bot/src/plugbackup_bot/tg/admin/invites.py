"""Инвайты беты: создание с лимитом и сроком, список, отзыв, кто воспользовался."""

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from plugbackup_bot import texts_ru as T
from plugbackup_bot.domain.roles import Actor, Permission
from plugbackup_bot.emoji import E
from plugbackup_bot.infra.clock import Clock
from plugbackup_bot.services.invites import (
    MAX_INVITES_AT_ONCE,
    InviteDetails,
    InviteService,
    InviteStatus,
    RevokeKind,
)
from plugbackup_bot.tg.admin import core
from plugbackup_bot.tg.admin.core import (
    Adm,
    admin_nav,
    cb,
    confirm_screen,
    notice_screen,
    pager,
    requires,
)
from plugbackup_bot.tg.format import fmt_date, fmt_datetime
from plugbackup_bot.tg.states import InviteStates
from plugbackup_bot.ui import buttons, cards
from plugbackup_bot.ui.markup import esc, fill
from plugbackup_bot.ui.pagination import PAGE_SIZE, Page
from plugbackup_bot.ui.screen import Screen, Ui

LIST = cb(core.INVITES, "list")
COUNT_CHOICES = (1, 5, 10, 20)
USES_CHOICES = (1, 3, 10, 50)
EXPIRY_CHOICES = (7, 30, 90)
MAX_COPY = 256


def _status(details_status: InviteStatus) -> str:
    return T.INV_STATUS[details_status.value]


# --- экраны ---


async def list_screen(invites: InviteService, clock: Clock, number: int) -> Screen:
    rows, total, active = await invites.page(limit=PAGE_SIZE, offset=number * PAGE_SIZE)
    pages = max(1, -(-total // PAGE_SIZE))
    if number >= pages:
        number = pages - 1
        rows, total, active = await invites.page(limit=PAGE_SIZE, offset=number * PAGE_SIZE)
    now = clock.now()
    items = [
        [
            buttons.plain(
                fill(
                    T.INV_ROW,
                    label=row.label,
                    uses=row.uses,
                    max=row.max_uses,
                    status=_status(row.status(now)),
                ),
                cb(core.INVITES, "card", i=row.id),
                E.invite,
            )
        ]
        for row in rows
    ]
    body = fill(T.INV_SUMMARY, total=total, active=active) if rows else T.INV_EMPTY
    page = Page(rows, max(number, 0), pages, total)
    markup = buttons.markup(
        [buttons.primary(T.BTN_INV_NEW, cb(core.INVITES, "n"), E.invite)],
        *items,
        pager(page, core.INVITES, "list"),
        admin_nav(cb(core.MENU)),
    )
    return Screen(cards.card(T.INV_TITLE, body, icon=E.invite), markup)


def _who(use) -> str:  # type: ignore[no-untyped-def]
    parts = [use.login or "", f"@{use.username}" if use.username else "", str(use.tg_id)]
    return " · ".join(part for part in parts if part)


def card_screen(
    details: InviteDetails, clock: Clock, tz: str, *, flash: str | None = None
) -> Screen:
    invite = details.invite
    now = clock.now()
    expiry = fmt_date(invite.expires_at, tz) if invite.expires_at is not None else T.INV_EXPIRY_NONE
    rows = [
        cards.plain_field(
            T.INV_CREATED,
            fill(
                T.INV_CREATED_VALUE,
                date=fmt_datetime(invite.created_at, tz),
                admin=invite.created_by,
            ),
        ),
        cards.plain_field(
            T.INV_USES, fill(T.INV_USES_VALUE, uses=invite.uses, max=invite.max_uses)
        ),
        cards.plain_field(T.INV_EXPIRES, expiry),
        cards.plain_field(T.INV_STATUS_FIELD, _status(invite.status(now))),
    ]
    if details.uses:
        uses = cards.bullets(
            fill(T.INV_USE_LINE, who=_who(use), at=fmt_datetime(use.used_at, tz))
            for use in details.uses
        )
    else:
        uses = T.INV_NOBODY
    text = cards.card(
        T.INV_CARD_TITLE.format(label=invite.label),
        cards.lines(*rows),
        cards.lines(f"<b>{T.INV_USED_BY}</b>", uses),
        icon=E.invite,
    )
    if flash:
        text = f"{flash}\n\n{text}"
    keyboard = []
    if invite.revoked_at is None:
        keyboard.append(
            [buttons.danger(T.BTN_INV_REVOKE, cb(core.INVITES, "rv", i=invite.id), E.trash)]
        )
    return Screen(text, buttons.markup(*keyboard, admin_nav(LIST)))


def created_screen(
    codes: list[str], max_uses: int, expires_days: int | None, clock: Clock, tz: str
) -> Screen:
    expiry = (
        fill(T.INV_EXPIRY_UNTIL, date=fmt_date(clock.now() + expires_days * 86400, tz))
        if expires_days
        else T.INV_EXPIRY_NONE
    )
    body = "\n".join(f"<code>{esc(code)}</code>" for code in codes)
    text = cards.card(
        T.INV_CREATED_TITLE,
        fill(T.INV_CREATED_BODY, count=len(codes), uses=max_uses, expiry=expiry),
        body,
        icon=E.ok,
    )
    rows = []
    joined = "\n".join(codes)
    if len(joined) <= MAX_COPY:
        rows.append([buttons.copy_button(T.BTN_INV_COPY_ALL, joined, E.invite)])
    return Screen(text, buttons.markup(*rows, admin_nav(LIST)))


# --- обработчики ---


@requires(Permission.INVITES)
async def cb_list(
    callback: CallbackQuery,
    callback_data: Adm,
    state: FSMContext,
    ui: Ui,
    invites: InviteService,
    clock: Clock,
    actor: Actor,
) -> None:
    await state.clear()
    await callback.answer()
    await ui.show(callback, await list_screen(invites, clock, callback_data.p))


@requires(Permission.INVITES)
async def cmd_invite(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    ui: Ui,
    invites: InviteService,
    clock: Clock,
    default_tz: str,
    actor: Actor,
) -> None:
    """/invite без числа открывает список, /invite N сразу создаёт N одноразовых кодов."""
    await state.clear()
    raw = (command.args or "").strip()
    if not raw:
        await ui.show(message, await list_screen(invites, clock, 0))
        return
    if not raw.isdecimal() or not 1 <= int(raw) <= MAX_INVITES_AT_ONCE:
        hint = T.INV_BAD_COUNT.format(max=MAX_INVITES_AT_ONCE)
        await ui.show(message, notice_screen(T.ADM_TYPE_ERROR.format(hint=hint), LIST))
        return
    codes = await invites.create(actor, int(raw))
    await ui.show(message, created_screen(codes, 1, None, clock, default_tz))


@requires(Permission.INVITES)
async def cb_card(
    callback: CallbackQuery,
    callback_data: Adm,
    ui: Ui,
    invites: InviteService,
    clock: Clock,
    default_tz: str,
    actor: Actor,
) -> None:
    await callback.answer()
    details = await invites.details(callback_data.i)
    if details is None:
        await ui.show(callback, notice_screen(T.ADM_NOT_FOUND, LIST))
        return
    await ui.show(callback, card_screen(details, clock, default_tz))


def _count_screen() -> Screen:
    rows = [
        [
            buttons.plain(T.BTN_INV_COUNT.format(n=n), cb(core.INVITES, "n2", i=n))
            for n in COUNT_CHOICES
        ]
    ]
    text = cards.card(
        T.INV_NEW_TITLE, fill(T.INV_ASK_COUNT, max=MAX_INVITES_AT_ONCE), icon=E.invite
    )
    return Screen(text, buttons.markup(*rows, admin_nav(LIST)))


@requires(Permission.INVITES)
async def cb_new(callback: CallbackQuery, state: FSMContext, ui: Ui, actor: Actor) -> None:
    await state.set_state(InviteStates.count)
    await callback.answer()
    await ui.show(callback, _count_screen())


@requires(Permission.INVITES)
async def msg_count(message: Message, state: FSMContext, ui: Ui, actor: Actor) -> None:
    raw = (message.text or "").strip()
    if not raw.isdecimal() or not 1 <= int(raw) <= MAX_INVITES_AT_ONCE:
        hint = T.INV_BAD_COUNT.format(max=MAX_INVITES_AT_ONCE)
        await ui.show(message, notice_screen(T.ADM_TYPE_ERROR.format(hint=hint), LIST))
        return
    await state.clear()
    await ui.show(message, _uses_screen(int(raw)))


def _uses_screen(count: int) -> Screen:
    rows = [
        [
            buttons.plain(T.BTN_INV_USES.format(n=n), cb(core.INVITES, "n3", i=count, p=n))
            for n in USES_CHOICES
        ]
    ]
    text = cards.card(T.INV_NEW_TITLE, fill(T.INV_ASK_USES, count=count), icon=E.invite)
    return Screen(text, buttons.markup(*rows, admin_nav(cb(core.INVITES, "n"))))


@requires(Permission.INVITES)
async def cb_uses(
    callback: CallbackQuery, callback_data: Adm, state: FSMContext, ui: Ui, actor: Actor
) -> None:
    await state.clear()
    await callback.answer()
    count = callback_data.i
    if not 1 <= count <= MAX_INVITES_AT_ONCE:
        await ui.show(callback, notice_screen(T.ADM_STALE, LIST))
        return
    await ui.show(callback, _uses_screen(count))


@requires(Permission.INVITES)
async def cb_expiry(callback: CallbackQuery, callback_data: Adm, ui: Ui, actor: Actor) -> None:
    await callback.answer()
    count, uses = callback_data.i, callback_data.p
    rows = [
        [buttons.plain(T.BTN_INV_NO_EXPIRY, cb(core.INVITES, "mk", i=count, p=uses, x="d0"))],
        [
            buttons.plain(
                T.BTN_INV_EXPIRY.format(days=days),
                cb(core.INVITES, "mk", i=count, p=uses, x=f"d{days}"),
            )
            for days in EXPIRY_CHOICES
        ],
    ]
    text = cards.card(
        T.INV_NEW_TITLE, fill(T.INV_ASK_EXPIRY, count=count, uses=uses), icon=E.invite
    )
    await ui.show(
        callback, Screen(text, buttons.markup(*rows, admin_nav(cb(core.INVITES, "n2", i=count))))
    )


@requires(Permission.INVITES)
async def cb_make(
    callback: CallbackQuery,
    callback_data: Adm,
    ui: Ui,
    invites: InviteService,
    clock: Clock,
    default_tz: str,
    actor: Actor,
) -> None:
    await callback.answer()
    raw = callback_data.x.removeprefix("d")
    days = int(raw) if raw.isdecimal() else -1
    try:
        codes = await invites.create(
            actor, callback_data.i, max_uses=callback_data.p, expires_days=days or None
        )
    except ValueError:
        await ui.show(callback, notice_screen(T.ADM_STALE, LIST))
        return
    await ui.show(callback, created_screen(codes, callback_data.p, days or None, clock, default_tz))


@requires(Permission.INVITES)
async def cb_revoke_ask(
    callback: CallbackQuery,
    callback_data: Adm,
    ui: Ui,
    invites: InviteService,
    actor: Actor,
) -> None:
    await callback.answer()
    details = await invites.details(callback_data.i)
    if details is None:
        await ui.show(callback, notice_screen(T.ADM_NOT_FOUND, LIST))
        return
    body = esc(T.INV_REVOKE_CONFIRM.format(label=details.invite.label))
    screen = confirm_screen(
        body,
        yes=cb(core.INVITES, "rv!", i=callback_data.i),
        back=cb(core.INVITES, "card", i=callback_data.i),
    )
    await ui.show(callback, screen)


@requires(Permission.INVITES)
async def cb_revoke(
    callback: CallbackQuery,
    callback_data: Adm,
    ui: Ui,
    invites: InviteService,
    clock: Clock,
    default_tz: str,
    actor: Actor,
) -> None:
    await callback.answer()
    result = await invites.revoke(callback_data.i, actor)
    details = await invites.details(callback_data.i)
    if details is None:
        await ui.show(callback, notice_screen(T.ADM_NOT_FOUND, LIST))
        return
    flash = (
        fill(T.INV_REVOKED, label=details.invite.label)
        if result is RevokeKind.OK
        else T.INV_REVOKE_ALREADY
    )
    await ui.show(callback, card_screen(details, clock, default_tz, flash=flash))


def register(router: Router) -> None:
    v = core.INVITES

    def only(action: str):  # type: ignore[no-untyped-def]
        return Adm.filter((F.s == v) & (F.a == action))

    router.message.register(cmd_invite, Command("invite"))
    router.callback_query.register(cb_list, only("list"))
    router.callback_query.register(cb_card, only("card"))
    router.callback_query.register(cb_new, only("n"))
    router.callback_query.register(cb_uses, only("n2"))
    router.callback_query.register(cb_expiry, only("n3"))
    router.callback_query.register(cb_make, only("mk"))
    router.callback_query.register(cb_revoke_ask, only("rv"))
    router.callback_query.register(cb_revoke, only("rv!"))
    router.message.register(
        msg_count, StateFilter(InviteStates.count), F.text, ~F.text.startswith("/")
    )
