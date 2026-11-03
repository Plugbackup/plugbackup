from aiogram import Bot


class TelegramStars:
    """Управление подписками Stars через Bot API (editUserStarSubscription)."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def set_canceled(self, tg_id: int, charge_id: str, canceled: bool) -> None:
        await self._bot.edit_user_star_subscription(
            user_id=tg_id, telegram_payment_charge_id=charge_id, is_canceled=canceled
        )
