from fakes import FakeClock
from helpers import NOW, add_client, add_node, add_user
from plugbackup_bot.db import Database, notifications
from plugbackup_bot.db import alerts as alerts_repo
from plugbackup_bot.db.usage import record_sample
from plugbackup_bot.services.maintenance import NOTIFICATION_RETENTION_S, prune_all

DAY = 24 * 3600


async def test_prune_removes_only_expired_rows(db: Database, clock: FakeClock) -> None:
    node_id = await add_node(db)
    await add_user(db, 1)
    client_id = await add_client(db, 1, node_id)
    async with db.transaction() as conn:
        await record_sample(conn, client_id, NOW - 31 * DAY, 1, 1)
        await record_sample(conn, client_id, NOW - 29 * DAY, 2, 1)
        await alerts_repo.replace_fail_counts(conn, client_id, {NOW - 31 * DAY: 5, NOW - DAY: 7})
        await alerts_repo.record_ip_hashes(
            conn, client_id, {"a" * 16: NOW - 31 * DAY, "b" * 16: NOW - DAY}, NOW, baseline=True
        )
        await notifications.claim(conn, client_id, "old", "k", NOW - NOTIFICATION_RETENTION_S - 1)
        await notifications.claim(conn, client_id, "new", "k", NOW - DAY)

    removed = await prune_all(db, clock)

    assert removed == {"usage_samples": 1, "login_data": 1, "notifications": 1}
    async with db.read() as conn:
        assert await alerts_repo.fail_counts(conn, client_id, 0) == {NOW - DAY: 7}
        assert await alerts_repo.known_hash_count(conn, client_id) == 1
        assert await notifications.last_sent(conn, client_id, "new") == NOW - DAY
        assert await notifications.last_sent(conn, client_id, "old") is None
