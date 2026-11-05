"""Получение доступа: бета по инвайту, выбор тарифа, публичный ключ."""

import logging
from collections.abc import Callable

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from plugbackup_bot import texts_ru as T
from plugbackup_bot.domain.keys import InvalidKey, KeyProblem, fingerprint, parse_public_key
from plugbackup_bot.domain.states import State
from plugbackup_bot.services.access import AccessService, KeyKind, OfferKind, RedeemKind
from plugbackup_bot.tg import keyboards, texts
from plugbackup_bot.tg.middlewares import AttemptLimiter
from plugbackup_bot.tg.states import AccessStates
from plugbackup_bot.ui import buttons
from plugbackup_bot.ui.buttons import markup
from plugbackup_bot.ui.screen import Screen, Ui

log = logging.getLogger(__name__)


def _home(text: str) -> Screen:
    return Screen(text, keyboards.only_home())


# --- бета ---


async def cb_beta(
    callback: CallbackQuery, state: FSMContext, access: AccessService, ui: Ui
) -> None:
    client = await access.live_client(callback.from_user.id)
    if client is not None and client.state is State.PENDING and client.ssh_key_body is None:
        # Инвайт принят, но ключа ещё нет: ведём дальше, а не отвечаем «хранилище уже есть».
        await state.set_state(AccessStates.waiting_key)
        await ui.show(callback, Screen(texts.pending_key_prompt(), keyboards.key_help()))
    elif client is not None:
        await ui.show(callback, _home(texts.INVITE_ERRORS[RedeemKind.HAS_CLIENT]))
    else:
        await ui.show(callback, Screen(texts.RISK_TEXT, keyboards.risk(keyboards.RISK_BETA)))
    await callback.answer()


async def msg_invite_code(
    message: Message,
    state: FSMContext,
    access: AccessService,
    invite_limiter: AttemptLimiter,
    ui: Ui,
) -> None:
    user_id = message.chat.id
    if invite_limiter.blocked(user_id):
        await ui.show(message, _home(T.TOO_MANY_CODES))
        return
    result = await access.redeem_invite(user_id, message.text or "")
    if result.kind is RedeemKind.OK and result.client is not None:
        invite_limiter.reset(user_id)
        await state.set_state(AccessStates.waiting_key)
        await ui.show(message, Screen(texts.beta_accepted(access.beta_plan), keyboards.key_help()))
        return
    if result.kind in (RedeemKind.INVALID, RedeemKind.USED):
        invite_limiter.fail(user_id)
    else:
        await state.clear()
    await ui.show(message, _home(texts.INVITE_ERRORS[result.kind]))


# --- тариф и оплата ---


async def cb_plan(callback: CallbackQuery, ui: Ui) -> None:
    """Выбор тарифа: сначала всегда экран с рисками."""
    plan_id = (callback.data or "").removeprefix(keyboards.PLAN_PREFIX)
    await ui.show(callback, Screen(texts.RISK_TEXT, keyboards.risk(plan_id)))
    await callback.answer()


async def cb_risk(
    callback: CallbackQuery, state: FSMContext, access: AccessService, bot: Bot, ui: Ui
) -> None:
    target = (callback.data or "").removeprefix(keyboards.RISK_PREFIX)
    user_id = callback.from_user.id
    await access.acknowledge_risks(user_id)
    await callback.answer()

    back = markup(buttons.nav(keyboards.MENU_TARIFFS))
    if target == keyboards.RISK_BETA:
        await state.set_state(AccessStates.waiting_invite)
        await ui.show(callback, _home(texts.beta_ask_code()))
        return

    offer = await access.offer_plan(user_id, target)
    if offer.kind is OfferKind.REJECTED and offer.reject is not None:
        await ui.show(callback, Screen(texts.REJECTIONS[offer.reject], back))
    elif offer.kind is OfferKind.OK and offer.plan is not None and offer.params is not None:
        try:
            link = await bot.create_invoice_link(**offer.params)  # type: ignore[arg-type]
        except TelegramAPIError:
            log.exception("could not create an invoice link")
            await ui.show(callback, Screen(T.OFFER_UNAVAILABLE, back))
            return
        await ui.show(
            callback,
            Screen(
                texts.plan_offer(offer.plan, replaces=offer.replaces_subscription),
                keyboards.pay(link, offer.plan),
            ),
        )
    else:
        await ui.show(callback, Screen(T.OFFER_UNAVAILABLE, back))


# --- ключ ---


async def process_key(
    message: Message,
    state: FSMContext,
    access: AccessService,
    kick_provisioning: Callable[[], None],
    ui: Ui,
) -> None:
    try:
        body = parse_public_key(message.text or "")
    except InvalidKey as error:
        if error.problem is KeyProblem.PRIVATE_KEY:
            try:
                await message.delete()
            except TelegramAPIError:
                log.debug("could not delete a message with a private key", exc_info=True)
        await ui.show(message, _home(texts.KEY_PROBLEMS[error.problem]))
        return

    result = await access.submit_key(message.chat.id, body)
    await state.clear()
    match result.kind:
        case KeyKind.ACCEPTED:
            await ui.show(message, _home(texts.key_accepted(fingerprint(body))))
            kick_provisioning()
        case KeyKind.REPLACED:
            await ui.show(message, _home(texts.key_replaced(fingerprint(body))))
            kick_provisioning()
        case KeyKind.UNCHANGED:
            await ui.show(message, _home(T.KEY_UNCHANGED))
        case KeyKind.BLOCKED:
            await ui.show(message, _home(T.KEY_BLOCKED))
        case KeyKind.NO_CLIENT:
            await ui.show(message, _home(T.KEY_NO_CLIENT))


async def _ask_for_key(
    event: Message | CallbackQuery, state: FSMContext, access: AccessService, ui: Ui
) -> None:
    client = await access.live_client(event.from_user.id)  # type: ignore[union-attr]
    if client is None:
        await ui.show(event, _home(T.KEY_NO_CLIENT))
        return
    await state.set_state(AccessStates.waiting_key)
    text = texts.KEY_HELP if client.ssh_key_body is None else texts.KEY_REPLACE_HELP
    await ui.show(event, Screen(text, keyboards.key_help()))


async def cmd_key(message: Message, state: FSMContext, access: AccessService, ui: Ui) -> None:
    await _ask_for_key(message, state, access, ui)


async def cb_key(callback: CallbackQuery, state: FSMContext, access: AccessService, ui: Ui) -> None:
    await _ask_for_key(callback, state, access, ui)
    await callback.answer()


async def msg_unexpected_text(
    message: Message,
    state: FSMContext,
    access: AccessService,
    kick_provisioning: Callable[[], None],
    ui: Ui,
) -> None:
    """Текст вне сценария. Если клиент ждёт выдачи доступа, считаем это ключом.

    Так сценарий переживает перезапуск бота, когда FSM-состояние в памяти потеряно.
    """
    if await access.awaiting_key(message.chat.id):
        await process_key(message, state, access, kick_provisioning, ui)
    else:
        await ui.show(message, _home(T.NOT_UNDERSTOOD))


def build_router() -> Router:
    router = Router(name="access")
    router.message.filter(F.chat.type == "private")
    router.callback_query.register(cb_beta, F.data == keyboards.MENU_BETA)
    router.callback_query.register(cb_plan, F.data.startswith(keyboards.PLAN_PREFIX))
    router.callback_query.register(cb_risk, F.data.startswith(keyboards.RISK_PREFIX))
    router.callback_query.register(cb_key, F.data == keyboards.MENU_KEY)
    router.message.register(cmd_key, Command("key"))
    # Команды (/start, /status…) не считаем кодом или ключом: пусть их обработают другие handlers.
    router.message.register(
        msg_invite_code, StateFilter(AccessStates.waiting_invite), F.text, ~F.text.startswith("/")
    )
    router.message.register(
        process_key, StateFilter(AccessStates.waiting_key), F.text, ~F.text.startswith("/")
    )
    # Должен быть последним: ловит любой обычный текст, который не подошёл выше.
    router.message.register(msg_unexpected_text, F.text, ~F.text.startswith("/"))
    return router
