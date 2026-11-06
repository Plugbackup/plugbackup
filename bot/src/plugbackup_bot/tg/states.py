from aiogram.fsm.state import State, StatesGroup


class AccessStates(StatesGroup):
    """Бот ждёт от клиента ввод: инвайт-код беты или публичный ключ."""

    waiting_invite = State()
    waiting_key = State()


class BroadcastStates(StatesGroup):
    """Администратор собирает рассылку: текст, кнопки, время."""

    text = State()
    buttons = State()
    when = State()


class MaintenanceStates(StatesGroup):
    """Администратор пишет текст для клиентов перед включением техработ."""

    text = State()


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


class SupportStates(StatesGroup):
    """Клиент пишет в поддержку."""

    message = State()


class SupportAdminStates(StatesGroup):
    """Администратор пишет ответ на обращение."""

    reply = State()
