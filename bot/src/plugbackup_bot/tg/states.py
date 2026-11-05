from aiogram.fsm.state import State, StatesGroup


class AccessStates(StatesGroup):
    """Бот ждёт от клиента ввод: инвайт-код беты или публичный ключ."""

    waiting_invite = State()
    waiting_key = State()


class AdminStates(StatesGroup):
    """Администратор пишет текст рассылки."""

    broadcast_text = State()


class ClientAdminStates(StatesGroup):
    """Администратор вводит текст или число в карточке клиента."""

    search = State()
    note = State()
    message = State()
    extend_days = State()
    bonus_gb = State()


class TariffStates(StatesGroup):
    """Администратор вводит новое значение тарифа."""

    stars = State()
    gb = State()
    rub = State()


class InviteStates(StatesGroup):
    count = State()


class NodeStates(StatesGroup):
    capacity = State()
    oversell = State()
    add_name = State()
    add_addr = State()
    add_host = State()
    add_capacity = State()
