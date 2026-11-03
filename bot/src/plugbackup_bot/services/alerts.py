"""Алерты безопасности клиента: проверка правил, тихие часы, кулдаун, доставка."""

import logging
from dataclasses import dataclass
from typing import Protocol

import aiosqlite
from aiogram.types import InlineKeyboardMarkup

from plugbackup_bot.db import Database, notifications
from plugbackup_bot.db import alerts as alerts_repo
from plugbackup_bot.db import usage as usage_repo
from plugbackup_bot.db.alerts import Prefs, Setting
from plugbackup_bot.db.clients import Client, list_by_state
from plugbackup_bot.db.nodes import Node, list_nodes
from plugbackup_bot.db.usage import Sample
from plugbackup_bot.domain import alerts as rules
from plugbackup_bot.domain.alerts import DAY, HOUR, Finding
from plugbackup_bot.domain.states import State
from plugbackup_bot.infra.clock import Clock
from plugbackup_bot.infra.storectl import Storectl, StorectlError
from plugbackup_bot.services.notify import Messenger

log = logging.getLogger(__name__)

Rendered = tuple[str, InlineKeyboardMarkup | None]
_RUNNING = (State.ACTIVE, State.EXPIRING)
# Первая выгрузка входов после включения алерта нового IP берёт неделю истории.
_BASELINE_WINDOW_S = 7 * DAY
# События сервиса актуальны сутки: дольше повторять попытки доставки бессмысленно.
_EVENT_WINDOW_S = DAY


class AlertRenderer(Protocol):
    def alert(self, finding: Finding, client: Client, tz: str) -> Rendered: ...

    def digest(
        self,
        client: Client,
        tz: str,
        *,
        used: int | None,
        quota: int,
        week_delta: int | None,
        last_change: int | None,
    ) -> Rendered: ...

    def service(self, kind: str, text: str, phase: str, client: Client) -> Rendered: ...


@dataclass
class AlertReport:
    evaluated: int = 0
    sent: int = 0
    errors: int = 0


class AlertEngine:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        storectl: Storectl,
        messenger: Messenger,
        renderer: AlertRenderer,
        default_tz: str,
    ) -> None:
        self._db = db
        self._clock = clock
        self._storectl = storectl
        self._messenger = messenger
        self._renderer = renderer
        self._default_tz = default_tz

    # --- правила по данным о месте и входах ---

    async def evaluate_all(self) -> AlertReport:
        """Проверяет всех работающих клиентов. Сбой на одном не мешает остальным."""
        now = self._clock.now()
        report = AlertReport()
        async with self._db.read() as conn:
            clients = await list_by_state(conn, *_RUNNING)
            nodes = {node.id: node for node in await list_nodes(conn)}
        for client in clients:
            node = nodes.get(client.node_id)
            if node is None:
                continue
            try:
                report.sent += await self._evaluate_client(client, node, now)
                report.evaluated += 1
            except Exception:
                report.errors += 1
                log.exception("alert evaluation failed for client %s", client.id)
        return report

    async def _evaluate_client(self, client: Client, node: Node, now: int) -> int:
        async with self._db.read() as conn:
            settings = await alerts_repo.get_settings(conn, client.tg_id)
            prefs = await alerts_repo.get_prefs(conn, client.tg_id, self._default_tz)
            samples = await usage_repo.history(conn, client.id, now - 30 * DAY)
            known_hashes = await alerts_repo.known_hash_count(conn, client.id)

        findings = _usage_findings(settings, samples, now)
        findings += await self._login_findings(client, node, settings, known_hashes, now)

        sent = 0
        for finding in findings:
            if await self._deliver(client, finding, prefs, now):
                sent += 1
                if finding.key == "new_ip":
                    await self._mark_ip_shown(client, finding, now)
        return sent

    async def _login_findings(
        self, client: Client, node: Node, settings: dict[str, Setting], known: int, now: int
    ) -> list[Finding]:
        fails_on, ip_on = settings["fails"].enabled, settings["new_ip"].enabled
        if not (fails_on or ip_on):
            return []
        baseline = ip_on and known == 0
        since = now - _BASELINE_WINDOW_S if baseline else (now // HOUR - 1) * HOUR
        try:
            events = await self._storectl.logins(node.addr, client.login, since)
        except StorectlError as error:
            log.warning("logins of %s: %s", client.login, error.kind.value)
            return []

        counts: dict[int, int] = {}
        seen: dict[str, int] = {}
        for event in events:
            if event.ok:
                seen[event.ip_hash] = max(seen.get(event.ip_hash, 0), event.ts)
            else:
                hour = event.ts // HOUR * HOUR
                counts[hour] = counts.get(hour, 0) + 1

        findings: list[Finding] = []
        async with self._db.transaction() as conn:
            if fails_on:
                await alerts_repo.replace_fail_counts(conn, client.id, counts)
                buckets = await alerts_repo.fail_counts(conn, client.id, now - 2 * HOUR)
                limit = settings["fails"].threshold or 0
                found = rules.eval_fails(buckets, limit, now)
                if found:
                    findings.append(found)
            if ip_on:
                await alerts_repo.record_ip_hashes(conn, client.id, seen, now, baseline=baseline)
                pending = await alerts_repo.new_ip_hashes(conn, client.id)
                if pending:
                    data: dict[str, int | str] = {
                        "label": pending[0][:8],
                        "count": len(pending),
                        "hashes": ",".join(pending),
                    }
                    findings.append(Finding("new_ip", True, data))
        return findings

    async def _mark_ip_shown(self, client: Client, finding: Finding, now: int) -> None:
        hashes = str(finding.data.get("hashes", "")).split(",")
        async with self._db.transaction() as conn:
            await alerts_repo.mark_ip_notified(conn, client.id, hashes, now)

    async def _deliver(self, client: Client, finding: Finding, prefs: Prefs, now: int) -> bool:
        """Тихие часы и кулдаун. False, если сообщение отложено или подавлено."""
        if not finding.critical and rules.is_quiet(now, prefs.tz, prefs.quiet_from, prefs.quiet_to):
            return False
        kind = f"alert:{finding.key}"
        async with self._db.transaction() as conn:
            last = await notifications.last_sent(conn, client.id, kind)
            if last is not None and now - last < prefs.cooldown_hours * HOUR:
                return False
            await notifications.claim(conn, client.id, kind, str(now), now)
        text, markup = self._renderer.alert(finding, client, prefs.tz)
        await self._messenger.send(client.tg_id, text, reply_markup=markup)
        return True

    # --- состояние сервиса ---

    async def process_service_events(self) -> int:
        """Рассылает события сервиса клиентам нод, у которых включён алерт «Состояние сервиса»."""
        now = self._clock.now()
        async with self._db.read() as conn:
            async with conn.execute(
                "SELECT * FROM service_events WHERE starts_at <= ? AND"
                " (starts_at > ? OR (ends_at IS NOT NULL AND ends_at > ?)) ORDER BY id",
                (now, now - _EVENT_WINDOW_S, now - _EVENT_WINDOW_S),
            ) as cur:
                events = [dict(row) for row in await cur.fetchall()]
            clients = await list_by_state(conn, *_RUNNING)
        sent = 0
        for event in events:
            for client in clients:
                if event["node_id"] is not None and event["node_id"] != client.node_id:
                    continue
                try:
                    sent += await self._notify_service(event, client, now)
                except Exception:
                    log.exception("service notice failed for client %s", client.id)
            async with self._db.transaction() as conn:
                await conn.execute(
                    "UPDATE service_events SET notified_at = ?"
                    " WHERE id = ? AND notified_at IS NULL",
                    (now, event["id"]),
                )
        return sent

    async def _notify_service(self, event: dict[str, object], client: Client, now: int) -> int:
        async with self._db.read() as conn:
            settings = await alerts_repo.get_settings(conn, client.tg_id)
            prefs = await alerts_repo.get_prefs(conn, client.tg_id, self._default_tz)
        if not settings["service"].enabled:
            return 0
        if rules.is_quiet(now, prefs.tz, prefs.quiet_from, prefs.quiet_to):
            return 0  # не критично: доставим после тихих часов, пока событие актуально
        sent = 0
        phases = ["start"]
        ends_at = event["ends_at"]
        if isinstance(ends_at, int) and ends_at <= now:
            phases.append("end")
        for phase in phases:
            key = f"{event['id']}:{phase}"
            async with self._db.transaction() as conn:
                if phase == "end" and not await _was_sent(conn, client.id, f"{event['id']}:start"):
                    continue  # о начале клиент не знает, конец ему ни к чему
                if not await notifications.claim(conn, client.id, "service", key, now):
                    continue
            text, markup = self._renderer.service(
                str(event["kind"]), str(event["text"]), phase, client
            )
            await self._messenger.send(client.tg_id, text, reply_markup=markup)
            sent += 1
        return sent

    # --- сводка за неделю ---

    async def weekly_digest(self) -> int:
        now = self._clock.now()
        async with self._db.read() as conn:
            clients = await list_by_state(conn, *_RUNNING)
        sent = 0
        for client in clients:
            try:
                sent += await self._digest_for(client, now)
            except Exception:
                log.exception("digest failed for client %s", client.id)
        return sent

    async def _digest_for(self, client: Client, now: int) -> int:
        async with self._db.read() as conn:
            settings = await alerts_repo.get_settings(conn, client.tg_id)
            prefs = await alerts_repo.get_prefs(conn, client.tg_id, self._default_tz)
        if not settings["weekly"].enabled or not rules.is_digest_time(now, prefs.tz):
            return 0
        if rules.is_quiet(now, prefs.tz, prefs.quiet_from, prefs.quiet_to):
            return 0
        async with self._db.read() as conn:
            samples = await usage_repo.history(conn, client.id, now - 8 * DAY)
        async with self._db.transaction() as conn:
            week = rules.week_key(now, prefs.tz)
            if not await notifications.claim(conn, client.id, "alert:weekly", week, now):
                return 0

        used = samples[-1].used_bytes if samples else None
        week_delta = None
        if samples:
            earlier = [s for s in samples if s.hour_ts <= now - 7 * DAY + 2 * HOUR]
            reference = earlier[-1] if earlier else samples[0]
            week_delta = samples[-1].used_bytes - reference.used_bytes
        text, markup = self._renderer.digest(
            client,
            prefs.tz,
            used=used,
            quota=client.quota_gb * 1024**3,
            week_delta=week_delta,
            last_change=rules.last_change_ts(samples),
        )
        await self._messenger.send(client.tg_id, text, reply_markup=markup)
        return 1


def _usage_findings(settings: dict[str, Setting], samples: list[Sample], now: int) -> list[Finding]:
    """Правила 1–4 по истории занятого места."""
    found: list[Finding | None] = []
    stale, drop, spike = settings["stale"], settings["drop"], settings["spike"]
    if stale.enabled and stale.threshold:
        found.append(rules.eval_stale(samples, stale.threshold, now))
    if drop.enabled and drop.threshold:
        found.append(rules.eval_drop(samples, drop.threshold, now))
    if spike.enabled and spike.threshold:
        found.append(rules.eval_spike(samples, spike.threshold, now))
    levels = [level for level in (80, 90, 100) if settings[f"fill_{level}"].enabled]
    if levels:
        found.append(rules.eval_fill(samples, levels, now))
    return [finding for finding in found if finding is not None]


async def _was_sent(conn: aiosqlite.Connection, client_id: int, key: str) -> bool:
    async with conn.execute(
        "SELECT 1 FROM notifications WHERE client_id = ? AND kind = 'service' AND dedupe_key = ?",
        (client_id, key),
    ) as cur:
        return await cur.fetchone() is not None
