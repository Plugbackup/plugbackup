"""Плановая чистка таблиц по срокам хранения."""

import logging

from plugbackup_bot.db import Database, notifications
from plugbackup_bot.db import alerts as alerts_repo
from plugbackup_bot.db import usage as usage_repo
from plugbackup_bot.infra.clock import Clock

log = logging.getLogger(__name__)

# Журнал уведомлений нужен для защиты от повторов, дольше года он ни к чему.
NOTIFICATION_RETENTION_S = 400 * 24 * 3600


async def prune_all(db: Database, clock: Clock) -> dict[str, int]:
    now = clock.now()
    async with db.transaction() as conn:
        removed = {
            "usage_samples": await usage_repo.prune(conn, now),
            "login_data": await alerts_repo.prune(conn, now),
            "notifications": await notifications.prune(conn, now - NOTIFICATION_RETENTION_S),
        }
    log.info("pruned: %s", removed)
    return removed
