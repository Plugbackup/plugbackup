import pytest
from aiogram.methods import CreateInvoiceLink

from helpers import CATALOG
from plugbackup_bot.domain.payload import make_payload, parse_payload
from plugbackup_bot.domain.plans import SUBSCRIPTION_PERIOD_S
from plugbackup_bot.services.billing import invoice_params


def test_payload_roundtrip() -> None:
    parsed = parse_payload(make_payload("gb50", 12))
    assert parsed is not None
    assert (parsed.plan_id, parsed.client_id) == ("gb50", 12)


def test_each_payload_has_its_own_nonce() -> None:
    assert make_payload("gb50") != make_payload("gb50")


def test_payload_fits_the_telegram_limit() -> None:
    longest = make_payload("a" * 16, 999_999_999)
    assert len(longest.encode()) <= 128


@pytest.mark.parametrize(
    "text",
    [
        "",
        "v2:gb50:0:abcdef01",
        "v1:gb50:0",
        "v1:GB50:0:abcdef01",
        "v1:gb50:-1:abcdef01",
        "v1:gb50:0:ABCDEF01",
        "v1:gb50:0:abcdef01\n",
        "v1:gb50:0:abcdef01:extra",
        "v1:" + "a" * 17 + ":0:abcdef01",
        "v1:gb50:1234567890:abcdef01",
    ],
)
def test_foreign_payloads_are_not_parsed(text: str) -> None:
    assert parse_payload(text) is None


@pytest.mark.parametrize("plan_id", ["", "GB", "a b", "a" * 17, "gb50:0"])
def test_make_payload_rejects_bad_plan(plan_id: str) -> None:
    with pytest.raises(ValueError):
        make_payload(plan_id)


def test_make_payload_rejects_bad_client() -> None:
    with pytest.raises(ValueError):
        make_payload("gb50", -1)


@pytest.mark.parametrize("plan_id", ["gb50", "gb100", "gb200"])
def test_invoice_matches_the_bot_api_rules(plan_id: str) -> None:
    plan = CATALOG.get(plan_id)
    params = invoice_params(plan, make_payload(plan_id))
    method = CreateInvoiceLink(**params)  # type: ignore[arg-type]
    assert method.currency == "XTR"
    assert method.provider_token in ("", None)
    assert method.subscription_period == SUBSCRIPTION_PERIOD_S == 2_592_000
    assert [(p.label, p.amount) for p in method.prices] == [(plan.name, plan.stars)]
    assert 1 <= len(method.title) <= 32
    assert 1 <= len(method.description) <= 255
    assert 1 <= len(method.payload.encode()) <= 128
