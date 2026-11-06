from aiogram import Bot


class TelegramStars:
    """Stars через Bot API: подписки (editUserStarSubscription) и возвраты (refundStarPayment)."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def set_canceled(self, tg_id: int, charge_id: str, canceled: bool) -> None:
        await self._bot.edit_user_star_subscription(
            user_id=tg_id, telegram_payment_charge_id=charge_id, is_canceled=canceled
        )

    async def refund(self, tg_id: int, charge_id: str) -> None:
        """Возвращает Stars за платёж (refundStarPayment)."""
        await self._bot.refund_star_payment(user_id=tg_id, telegram_payment_charge_id=charge_id)
