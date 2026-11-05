"""Оплата Stars: pre_checkout, successful_payment и события подписки.

Зависимости (`billing`, `alerter`, `messenger`, `default_tz`) приходят из workflow_data
диспетчера.
"""

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BotSubscriptionUpdated, Message, PreCheckoutQuery

from plugbackup_bot import texts_ru as T
from plugbackup_bot.services.billing import (
    BillingService,
    PaymentEvent,
    PaymentKind,
    PaymentOutcome,
)
from plugbackup_bot.services.notify import AdminAlerter, Messenger
from plugbackup_bot.tg import keyboards, texts
from plugbackup_bot.tg.format import fmt_date
from plugbackup_bot.tg.states import AccessStates
from plugbackup_bot.ui import banners
from plugbackup_bot.ui.screen import Screen, Ui

log = logging.getLogger(__name__)


async def on_pre_checkout(query: PreCheckoutQuery, billing: BillingService) -> None:
    try:
        rejection = await billing.check_pre_checkout(
            tg_id=query.from_user.id,
            currency=query.currency,
            total_amount=query.total_amount,
            payload=query.invoice_payload,
        )
    except Exception:
        log.exception("pre_checkout failed")
        await query.answer(ok=False, error_message=T.TRY_LATER)
        return
    if rejection is None:
        await query.answer(ok=True)
    else:
        log.info("pre_checkout of %s rejected: %s", query.from_user.id, rejection.value)
        await query.answer(ok=False, error_message=texts.REJECTIONS[rejection])


async def on_successful_payment(
    message: Message,
    state: FSMContext,
    billing: BillingService,
    alerter: AdminAlerter,
    default_tz: str,
    ui: Ui,
) -> None:
    payment = message.successful_payment
    if payment is None or message.from_user is None:
        return
    event = PaymentEvent(
        tg_id=message.from_user.id,
        currency=payment.currency,
        total_amount=payment.total_amount,
        payload=payment.invoice_payload,
        charge_id=payment.telegram_payment_charge_id,
        provider_charge_id=payment.provider_payment_charge_id or None,
        is_recurring=bool(payment.is_recurring),
        is_first=bool(payment.is_first_recurring),
        sub_expires_at=payment.subscription_expiration_date,
    )
    outcome = await billing.handle_payment(event)

    if outcome.admin_alert:
        await alerter.alert(outcome.admin_alert)
    if outcome.cancel_charge_id and not await billing.disable_subscription(
        event.tg_id, outcome.cancel_charge_id
    ):
        await alerter.alert(
            "Не удалось отключить прежнюю подписку после смены тарифа, возможно двойное списание.\n"
            f"tg_id: {event.tg_id}, charge_id: {outcome.cancel_charge_id}"
        )

    for screen in _screens(outcome, default_tz):
        await ui.show(message, screen)
    if outcome.ask_key:
        await state.set_state(AccessStates.waiting_key)


def _screens(outcome: PaymentOutcome, tz: str) -> list[Screen]:
    """Что показать после платежа. Новый тариф и смена тарифа идут с баннером и эффектом,
    подсказка про ключ отдельным сообщением: подпись к фото короче 1024 знаков."""
    client = outcome.client
    until = fmt_date(client.expires_at, tz) if client and client.expires_at else "—"
    key_help = Screen(texts.KEY_HELP, keyboards.key_help())
    match outcome.kind:
        case PaymentKind.NEW_CLIENT if outcome.plan:
            text = texts.paid_new(outcome.plan, until)
            return [Screen(text, banner=banners.PAID, effect=True), key_help]
        case PaymentKind.PLAN_CHANGED if outcome.plan:
            text = texts.plan_changed(
                outcome.plan, until, resumed=outcome.resumed, ask_key=outcome.ask_key
            )
            screens = [Screen(text, banner=banners.PAID, effect=True)]
            return [*screens, key_help] if outcome.ask_key else screens
        case PaymentKind.RENEWED:
            text = texts.renewed(until, resumed=outcome.resumed)
            return [Screen(text, keyboards.only_home())]
        case PaymentKind.DUPLICATE:
            return [key_help] if outcome.ask_key else []
        case PaymentKind.ANOMALY:
            return [Screen(T.PAYMENT_MANUAL, keyboards.only_home())]
    return []


async def on_subscription(
    update: BotSubscriptionUpdated,
    billing: BillingService,
    messenger: Messenger,
    alerter: AdminAlerter,
    default_tz: str,
) -> None:
    outcome = await billing.handle_subscription_update(
        update.user.id, update.invoice_payload, update.state
    )
    if outcome is None:
        return
    client = outcome.client
    until = fmt_date(client.expires_at, default_tz) if client.expires_at else "—"
    if outcome.state == "canceled":
        await messenger.send(client.tg_id, texts.renewal_canceled(until))
    elif outcome.state == "active":
        await messenger.send(client.tg_id, T.RENEWAL_ENABLED)
    elif outcome.state == "failed":
        await messenger.send(client.tg_id, texts.renewal_failed(until))
        await alerter.alert(
            f"Сбой списания подписки: логин {client.login}, tg_id {client.tg_id}, "
            f"тариф {client.plan_id}."
        )


def build_router() -> Router:
    router = Router(name="payments")
    router.pre_checkout_query.register(on_pre_checkout)
    router.message.register(on_successful_payment, F.successful_payment)
    router.subscription.register(on_subscription)
    return router
