"""Роли администраторов и права. Правила чистые: ничего не читают и не пишут.

owner видит и делает всё. support: клиенты (без продления, смены тарифа, бонусов и удаления),
тикеты и заявки на восстановление. Права проверяются на сервере при каждом действии.
"""

from dataclasses import dataclass
from enum import StrEnum


class Role(StrEnum):
    OWNER = "owner"
    SUPPORT = "support"


class Permission(StrEnum):
    PANEL = "panel"  # открыть /admin и отменить ввод: есть у любого администратора
    DASHBOARD = "dashboard"
    CLIENTS_VIEW = "clients_view"
    CLIENTS_NOTE = "clients_note"
    CLIENTS_MESSAGE = "clients_message"
    CLIENTS_BLOCK = "clients_block"
    CLIENTS_EXTEND = "clients_extend"
    CLIENTS_PLAN = "clients_plan"
    CLIENTS_BONUS = "clients_bonus"
    CLIENTS_DELETE = "clients_delete"
    TARIFFS = "tariffs"
    INVITES = "invites"
    BROADCASTS = "broadcasts"
    NODES = "nodes"
    MAINTENANCE = "maintenance"
    TICKETS = "tickets"
    RESTORE = "restore"
    PAYMENTS = "payments"
    REFUNDS = "refunds"
    ADMINS = "admins"
    JOURNAL = "journal"
    EXPORT = "export"
    BACKUP = "backup"
    EMOJI = "emoji"


_SUPPORT = frozenset(
    {
        Permission.PANEL,
        Permission.CLIENTS_VIEW,
        Permission.CLIENTS_NOTE,
        Permission.CLIENTS_MESSAGE,
        Permission.CLIENTS_BLOCK,
        Permission.TICKETS,
        Permission.RESTORE,
    }
)

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.OWNER: frozenset(Permission),
    Role.SUPPORT: _SUPPORT,
}


def can(role: Role, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS[role]


@dataclass(frozen=True, slots=True)
class Actor:
    """Кто выполняет действие: Telegram id и роль на момент действия."""

    tg_id: int
    role: Role

    def can(self, permission: Permission) -> bool:
        return can(self.role, permission)

    @property
    def tag(self) -> str:
        """Метка для журнала переходов состояния (state_log.actor)."""
        return f"admin:{self.tg_id}"
