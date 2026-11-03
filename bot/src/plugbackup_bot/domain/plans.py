"""Тарифы. Названия и объёмы берутся из design/data/plans.json, цены в Stars из .env."""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

GIB = 1024**3
# Telegram принимает для подписки Stars только период в 30 дней.
SUBSCRIPTION_PERIOD_S = 2_592_000

_PLAN_ID_RE = re.compile(r"^[a-z0-9_]{1,16}$")


class PlanConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Plan:
    id: str
    name: str
    gb: int
    price_rub: int
    stars: int

    @property
    def is_beta(self) -> bool:
        return self.price_rub == 0

    @property
    def quota_bytes(self) -> int:
        # refquota в ZFS считается в GiB, поэтому «50 ГБ» это 50 * 1024**3.
        return self.gb * GIB


class PlanCatalog:
    def __init__(self, plans: Sequence[Plan]) -> None:
        ids = [plan.id for plan in plans]
        if len(set(ids)) != len(ids):
            raise PlanConfigError("идентификаторы тарифов повторяются")
        betas = [plan for plan in plans if plan.is_beta]
        paid = sorted((plan for plan in plans if not plan.is_beta), key=lambda plan: plan.gb)
        if len(betas) != 1:
            raise PlanConfigError("нужен ровно один бесплатный тариф (бета)")
        if not paid:
            raise PlanConfigError("нет платных тарифов")
        if len({plan.gb for plan in paid}) != len(paid):
            raise PlanConfigError("у платных тарифов должны быть разные объёмы")
        self._by_id = {plan.id: plan for plan in plans}
        self.beta = betas[0]
        self.paid: tuple[Plan, ...] = tuple(paid)

    def find(self, plan_id: str) -> Plan | None:
        return self._by_id.get(plan_id)

    def get(self, plan_id: str) -> Plan:
        plan = self._by_id.get(plan_id)
        if plan is None:
            raise KeyError(plan_id)
        return plan


def load_catalog(path: Path, prices_stars: Mapping[str, int]) -> PlanCatalog:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        items = raw["plans"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PlanConfigError(f"не удалось прочитать тарифы из {path}: {exc}") from exc

    plans: list[Plan] = []
    for item in items:
        try:
            plan_id = str(item["id"])
            name = str(item["name"])
            gb = int(item["gb"])
            price_rub = int(item["price_rub"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PlanConfigError(f"некорректная запись тарифа: {item!r}") from exc
        if not _PLAN_ID_RE.fullmatch(plan_id):
            raise PlanConfigError(f"недопустимый id тарифа: {plan_id!r}")
        if gb <= 0 or price_rub < 0:
            raise PlanConfigError(f"тариф {plan_id}: объём и цена должны быть положительными")
        stars = 0
        if price_rub > 0:
            if plan_id not in prices_stars:
                raise PlanConfigError(f"в PRICES_STARS нет цены для тарифа {plan_id}")
            stars = prices_stars[plan_id]
        plans.append(Plan(id=plan_id, name=name, gb=gb, price_rub=price_rub, stars=stars))

    unknown = set(prices_stars) - {plan.id for plan in plans if not plan.is_beta}
    if unknown:
        raise PlanConfigError(f"в PRICES_STARS лишние тарифы: {', '.join(sorted(unknown))}")
    return PlanCatalog(plans)
