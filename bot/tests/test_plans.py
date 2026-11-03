import json
from pathlib import Path

import pytest

from plugbackup_bot.domain.plans import GIB, Plan, PlanCatalog, PlanConfigError, load_catalog

REPO_PLANS = Path(__file__).resolve().parents[2] / "design" / "data" / "plans.json"
PRICES = {"gb50": 50, "gb100": 100, "gb200": 170}


def plan_entry(plan_id: str, gb: int, price_rub: int) -> dict[str, object]:
    return {"id": plan_id, "name": f"{gb} ГБ", "gb": gb, "price_rub": price_rub}


def write(tmp_path: Path, plans: list[dict[str, object]]) -> Path:
    path = tmp_path / "plans.json"
    path.write_text(json.dumps({"plans": plans}), encoding="utf-8")
    return path


GOOD = [
    plan_entry("beta", 10, 0),
    plan_entry("gb50", 50, 79),
    plan_entry("gb100", 100, 149),
    plan_entry("gb200", 200, 249),
]


@pytest.mark.skipif(not REPO_PLANS.exists(), reason="design/data/plans.json нет рядом с ботом")
def test_repository_plans_file_loads() -> None:
    catalog = load_catalog(REPO_PLANS, PRICES)
    assert catalog.beta.id == "beta"
    assert catalog.beta.gb == 10
    assert [plan.id for plan in catalog.paid] == ["gb50", "gb100", "gb200"]
    assert [plan.stars for plan in catalog.paid] == [50, 100, 170]


def test_catalog_lookup_and_quota(tmp_path: Path) -> None:
    catalog = load_catalog(write(tmp_path, GOOD), PRICES)
    assert catalog.get("gb50").quota_bytes == 50 * GIB
    assert catalog.beta.is_beta
    assert catalog.beta.stars == 0
    assert not catalog.get("gb100").is_beta
    assert catalog.find("nope") is None
    with pytest.raises(KeyError):
        catalog.get("nope")


def test_paid_plans_are_sorted_by_size(tmp_path: Path) -> None:
    shuffled = [GOOD[3], GOOD[0], GOOD[2], GOOD[1]]
    catalog = load_catalog(write(tmp_path, shuffled), PRICES)
    assert [plan.gb for plan in catalog.paid] == [50, 100, 200]


def test_missing_price_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(PlanConfigError, match="gb200"):
        load_catalog(write(tmp_path, GOOD), {"gb50": 50, "gb100": 100})


def test_extra_price_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(PlanConfigError, match="gb500"):
        load_catalog(write(tmp_path, GOOD), {**PRICES, "gb500": 400})


def test_price_for_beta_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(PlanConfigError, match="beta"):
        load_catalog(write(tmp_path, GOOD), {**PRICES, "beta": 1})


@pytest.mark.parametrize(
    "plans",
    [
        [plan_entry("gb50", 50, 79)],
        [plan_entry("beta", 10, 0), plan_entry("free", 5, 0), plan_entry("gb50", 50, 79)],
        [plan_entry("beta", 10, 0)],
        [plan_entry("beta", 10, 0), plan_entry("gb50", 50, 79), plan_entry("gb50", 60, 99)],
        [plan_entry("beta", 10, 0), plan_entry("gb50", 50, 79), plan_entry("gb51", 50, 99)],
        [plan_entry("beta", 10, 0), plan_entry("gb50", 0, 79)],
        [plan_entry("beta", 10, 0), plan_entry("GB 50", 50, 79)],
    ],
)
def test_broken_catalogs_are_rejected(tmp_path: Path, plans: list[dict[str, object]]) -> None:
    with pytest.raises(PlanConfigError):
        load_catalog(write(tmp_path, plans), {"gb50": 50, "gb51": 60, "free": 1})


def test_unreadable_files_are_reported(tmp_path: Path) -> None:
    with pytest.raises(PlanConfigError):
        load_catalog(tmp_path / "missing.json", PRICES)
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(PlanConfigError):
        load_catalog(broken, PRICES)


def test_catalog_requires_unique_ids() -> None:
    twin = Plan(id="a", name="A", gb=1, price_rub=0, stars=0)
    with pytest.raises(PlanConfigError):
        PlanCatalog([twin, twin])
