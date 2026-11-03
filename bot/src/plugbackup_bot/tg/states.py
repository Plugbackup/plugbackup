from aiogram.fsm.state import State, StatesGroup


class AccessStates(StatesGroup):
    """Бот ждёт от клиента ввод: инвайт-код беты или публичный ключ."""

    waiting_invite = State()
    waiting_key = State()


class AdminStates(StatesGroup):
    """Администратор пишет текст рассылки."""

    broadcast_text = State()
