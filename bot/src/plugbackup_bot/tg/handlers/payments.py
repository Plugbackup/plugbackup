"""Оплата Stars: pre_checkout, successful_payment и события подписки.

Зависимости (`billing`, `alerter`, `messenger`, `default_tz`) приходят из workflow_data
диспетчера.
"""

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BotSubscriptionUpdated, Message, PreCheckoutQuery

from plugbackup_bot.services.billing import (
    BillingService,
    PaymentEvent,
    PaymentKind,
    PaymentOutcome,
)
from plugbackup_bot.services.notify import AdminAlerter, Messenger
from plugbackup_bot.tg import texts
from plugbackup_bot.tg.format import fmt_date
from plugbackup_bot.tg.states import AccessStates

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
        await query.answer(ok=False, error_message=texts.TRY_LATER)
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

    text = _reply_text(outcome, default_tz)
    if text is not None:
        await message.answer(text)
    if outcome.ask_key:
        await state.set_state(AccessStates.waiting_key)


def _reply_text(outcome: PaymentOutcome, tz: str) -> str | None:
    client = outcome.client
    until = fmt_date(client.expires_at, tz) if client and client.expires_at else "—"
    match outcome.kind:
        case PaymentKind.NEW_CLIENT if outcome.plan:
            return texts.paid_new(outcome.plan, until)
        case PaymentKind.PLAN_CHANGED if outcome.plan:
            return texts.plan_changed(
                outcome.plan, until, resumed=outcome.resumed, ask_key=outcome.ask_key
            )
        case PaymentKind.RENEWED:
            return texts.renewed(until, resumed=outcome.resumed)
        case PaymentKind.DUPLICATE:
            return texts.KEY_HELP if outcome.ask_key else None
        case PaymentKind.ANOMALY:
            return texts.PAYMENT_MANUAL
    return None


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
        await messenger.send(client.tg_id, texts.RENEWAL_ENABLED)
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
