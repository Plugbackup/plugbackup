"""Реакции на результат операций storectl: сообщения клиенту и алерты админу."""

from plugbackup_bot.db import Database
from plugbackup_bot.db.clients import Client
from plugbackup_bot.db.jobs import Job, Op
from plugbackup_bot.db.nodes import get_node
from plugbackup_bot.infra.storectl import StorectlError
from plugbackup_bot.services.notify import AdminAlerter, Messenger
from plugbackup_bot.tg import texts


class ClientHooks:
    def __init__(self, db: Database, messenger: Messenger, alerter: AdminAlerter) -> None:
        self._db = db
        self._messenger = messenger
        self._alerter = alerter

    async def job_done(self, job: Job, client: Client) -> None:
        if job.op is not Op.ADD:
            return
        if job.params.get("replace"):
            await self._messenger.send(client.tg_id, texts.KEY_CHANGED_NOTICE)
            return
        async with self._db.read() as conn:
            node = await get_node(conn, client.node_id)
        if node is not None:
            await self._messenger.send(client.tg_id, texts.access_ready(client, node))

    async def job_problem(
        self, job: Job, client: Client | None, error: StorectlError, *, final: bool
    ) -> None:
        who = client.login if client else f"клиент {job.client_id}"
        tail = "Повторов не будет, нужен ручной разбор." if final else "Повторяем."
        await self._alerter.alert(f"Ошибка storectl {job.op.value} для {who}: {error}. {tail}")
        first_access = job.op is Op.ADD and not job.params.get("replace") and client is not None
        if first_access and client is not None:
            if final:
                await self._messenger.send(client.tg_id, texts.ACCESS_FAILED)
            elif job.attempts == 0:
                await self._messenger.send(client.tg_id, texts.ACCESS_DELAYED)
