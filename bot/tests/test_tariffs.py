"""Тарифы в базе: правки админа, «живой» каталог, цены и видимость для покупателей и подписчиков."""

import pytest
import pytest_asyncio
from aiogram.methods import AnswerCallbackQuery, CreateInvoiceLink

from fakes import FakeClock
from harness import ADMIN, SUPPORT, USER, Harness
from helpers import NOW, add_client, add_user, fake_key_body, plain
from plugbackup_bot import texts_ru as T
from plugbackup_bot.db import Database
from plugbackup_bot.db import audit as audit_repo
from plugbackup_bot.db import plans as plans_repo
from plugbackup_bot.db.clients import get_client
from plugbackup_bot.domain.plans import Plan, PlanCatalog
from plugbackup_bot.domain.roles import Actor, Role
from plugbackup_bot.services.billing import Reject
from plugbackup_bot.services.plans import PlanResult, PlanService
from plugbackup_bot.tg import texts
from plugbackup_bot.tg.admin import core
from plugbackup_bot.tg.admin.core import cb

OWNER = Actor(ADMIN.id, Role.OWNER)
TAR = core.TARIFFS
KEY = fake_key_body(0)


@pytest_asyncio.fixture
async def h(db: Database, clock: FakeClock) -> Harness:
    harness = await Harness.build(db, clock)
    await harness.directory.add_support(OWNER, SUPPORT.id)
    async with db.transaction() as conn:
        await conn.execute("DELETE FROM audit_log")
    return harness


async def stored(h: Harness, plan_id: str) -> Plan:
    async with h.db.read() as conn:
        plan = await plans_repo.get_plan(conn, plan_id)
    assert plan is not None
    return plan


async def audit_actions(h: Harness) -> list[tuple[str, dict]]:  # type: ignore[type-arg]
    async with h.db.read() as conn:
        entries = await audit_repo.page(conn, limit=50, offset=0)
    return [(e.action, e.details) for e in reversed(entries)]


# --- база и каталог ---


async def test_the_first_start_fills_the_table_and_later_starts_keep_admin_edits(
    h: Harness,
) -> None:
    async with h.db.read() as conn:
        assert [p.id for p in await plans_repo.list_plans(conn)] == [
            "beta",
            "gb50",
            "gb100",
            "gb200",
        ]
    await h.plans.set_stars("gb50", OWNER, 77)
    again = await h.plans.seed(h.catalog.plans)  # перезапуск: файл снова читается
    assert again == 0
    assert (await stored(h, "gb50")).stars == 77  # правка админа важнее файла


async def test_a_new_plan_in_the_file_is_added_on_the_next_start(h: Harness) -> None:
    extra = Plan(id="gb500", name="500 ГБ", gb=500, price_rub=599, stars=400)
    assert await h.plans.seed([*h.catalog.plans, extra]) == 1
    await h.plans.load()
    assert h.catalog.find("gb500") is not None
    assert [p.id for p in h.catalog.paid][-1] == "gb500"


async def test_the_catalog_object_is_updated_in_place_for_every_service(h: Harness) -> None:
    same = h.catalog
    await h.plans.set_stars("gb100", OWNER, 120)
    assert h.catalog is same and h.catalog.get("gb100").stars == 120
    assert h.access._catalog is same  # те же объекты видят правку сразу, без перезапуска


async def test_catalog_validation_still_protects_the_invariants() -> None:
    from plugbackup_bot.domain.plans import PlanConfigError

    base = [
        Plan("beta", "Бета", 10, 0, 0),
        Plan("a", "A", 50, 79, 50),
    ]
    catalog = PlanCatalog(base)
    with pytest.raises(PlanConfigError):
        catalog.replace([*base, Plan("b", "B", 50, 99, 60)])  # одинаковые объёмы
    with pytest.raises(PlanConfigError):
        catalog.replace([base[0]])  # нет платных
    assert catalog.get("a").gb == 50  # неудачная замена каталог не портит


# --- правки ---


async def test_stars_price_changes_for_new_purchases_and_is_logged(h: Harness) -> None:
    assert await h.plans.set_stars("gb50", OWNER, 60) is PlanResult.OK
    assert h.catalog.get("gb50").stars == 60
    assert await audit_actions(h) == [("tariff.stars", {"after": 60, "before": 50})]


@pytest.mark.parametrize("stars", [0, -5, 10_001])
async def test_silly_prices_are_refused(h: Harness, stars: int) -> None:
    assert await h.plans.set_stars("gb50", OWNER, stars) is PlanResult.INVALID
    assert h.catalog.get("gb50").stars == 50 and not await audit_actions(h)


async def test_volume_change_renames_an_auto_named_plan_and_keeps_a_custom_name(h: Harness) -> None:
    assert await h.plans.set_gb("gb50", OWNER, 60) is PlanResult.OK
    assert (h.catalog.get("gb50").gb, h.catalog.get("gb50").name) == (60, "60 ГБ")
    async with h.db.transaction() as conn:
        await plans_repo.update_fields(conn, "gb100", NOW, name="Семейный")
    await h.plans.load()
    await h.plans.set_gb("gb100", OWNER, 120)
    assert h.catalog.get("gb100").name == "Семейный"


async def test_volume_must_stay_unique_among_paid_plans(h: Harness) -> None:
    assert await h.plans.set_gb("gb50", OWNER, 100) is PlanResult.DUPLICATE_GB
    assert h.catalog.get("gb50").gb == 50
    assert await h.plans.set_gb("beta", OWNER, 50) is PlanResult.OK  # бета с платными не спорит


async def test_the_beta_keeps_its_price_and_visibility(h: Harness) -> None:
    for result in (
        await h.plans.set_stars("beta", OWNER, 5),
        await h.plans.set_price_rub("beta", OWNER, 5),
        await h.plans.set_visible("beta", OWNER, False),
    ):
        assert result is PlanResult.BETA
    assert h.catalog.beta.is_beta and h.catalog.beta.visible
    assert await h.plans.set_gb("beta", OWNER, 15) is PlanResult.OK


async def test_unknown_plans_are_not_found(h: Harness) -> None:
    assert await h.plans.set_stars("nope", OWNER, 5) is PlanResult.NOT_FOUND


# --- что видят покупатели ---


async def test_a_hidden_plan_disappears_from_the_tariff_list_but_stays_known(h: Harness) -> None:
    await h.plans.set_visible("gb100", OWNER, False)
    assert [p.id for p in h.catalog.paid] == ["gb50", "gb200"]
    assert h.catalog.find("gb100") is not None  # у клиентов на нём всё работает
    await h.press("menu:tariffs")
    assert "100 ГБ" not in plain(h.last().text)
    assert not any(data == "plan:gb100" for _, data, _ in h.last_buttons())


async def test_all_tariffs_hidden_is_explained_to_buyers(h: Harness) -> None:
    for plan_id in ("gb50", "gb100", "gb200"):
        await h.plans.set_visible(plan_id, OWNER, False)
    await h.press("menu:tariffs")
    assert T.TARIFFS_NONE in plain(h.last().text)


async def test_a_hidden_plan_cannot_be_bought_even_with_an_old_button(h: Harness) -> None:
    await h.plans.set_visible("gb100", OWNER, False)
    await h.press("risk:gb100")
    assert h.last().text == texts.REJECTIONS[Reject.BAD_PLAN]
    assert not h.session.of(CreateInvoiceLink)


async def test_the_new_price_is_used_for_new_invoices_and_old_ones_are_refused(h: Harness) -> None:
    await h.press("risk:gb50")
    old_invoice = h.session.of(CreateInvoiceLink)[-1]
    assert old_invoice.prices[0].amount == 50
    await h.plans.set_stars("gb50", OWNER, 65)
    reject = await h.billing.check_pre_checkout(
        tg_id=USER.id, currency="XTR", total_amount=50, payload=old_invoice.payload
    )
    assert reject is Reject.PRICE_CHANGED  # ссылка на оплату со старой ценой устарела
    await h.press("risk:gb50")
    assert h.session.of(CreateInvoiceLink)[-1].prices[0].amount == 65


async def test_a_new_buyer_gets_the_new_volume_and_old_clients_keep_theirs(h: Harness) -> None:
    await add_user(h.db, 7)
    old = await add_client(h.db, 7, 1, ssh_key_body=KEY)  # клиент на 50 ГБ
    await h.plans.set_gb("gb50", OWNER, 60)

    await h.press("risk:gb50")
    invoice = h.session.of(CreateInvoiceLink)[-1]
    await _pay(h, invoice.payload, invoice.prices[0].amount)
    newcomer = await h.access.live_client(USER.id)
    async with h.db.read() as conn:
        previous = await get_client(conn, old)
    assert newcomer is not None and newcomer.quota_gb == 60  # новая покупка по новому объёму
    assert previous is not None and previous.quota_gb == 50  # у действующего клиента без изменений


async def test_renewals_pass_at_the_price_the_subscription_was_bought_for(h: Harness) -> None:
    await h.press("risk:gb50")
    invoice = h.session.of(CreateInvoiceLink)[-1]
    await _pay(h, invoice.payload, 50)

    await h.plans.set_stars("gb50", OWNER, 90)  # цену подняли
    await h.plans.set_visible("gb50", OWNER, False)  # и тариф скрыли
    renewal = await h.billing.check_pre_checkout(
        tg_id=USER.id, currency="XTR", total_amount=50, payload=invoice.payload
    )
    assert renewal is None  # подписчика это не затрагивает
    wrong_amount = await h.billing.check_pre_checkout(
        tg_id=USER.id, currency="XTR", total_amount=51, payload=invoice.payload
    )
    assert wrong_amount is Reject.BAD_PLAN  # скрытый тариф с чужой суммой по-прежнему не продаётся


async def _pay(h: Harness, payload: str, amount: int) -> None:
    from datetime import UTC, datetime

    from aiogram.types import Chat, Message, SuccessfulPayment

    h._update_id += 1
    message = Message(
        message_id=h._update_id + 500,
        date=datetime.now(UTC),
        chat=Chat(id=USER.id, type="private"),
        from_user=USER,
        successful_payment=SuccessfulPayment(
            currency="XTR",
            total_amount=amount,
            invoice_payload=payload,
            telegram_payment_charge_id=f"charge-{h._update_id}",
            provider_payment_charge_id="prov",
            subscription_expiration_date=h.clock.now() + 2_592_000,
            is_recurring=True,
            is_first_recurring=True,
        ),
    )
    await h._feed(message=message)


# --- экраны админа ---


async def test_the_tariff_list_and_card(h: Harness) -> None:
    await h.say("/tariffs", user=ADMIN)
    text = plain(h.last().text)
    assert "50 ГБ — 79 ₽ · 50 Stars · виден" in text
    assert "Бета — бесплатно, 10 ГБ" in text
    assert "новые покупки" in text
    await h.press(cb(TAR, "card", x="gb50"), user=ADMIN)
    card = plain(h.last().text)
    assert "Тариф 50 ГБ" in card and "Цена в Stars: 50" in card and "Клиентов на тарифе: 0" in card
    assert "новые покупки" in card


async def test_editing_a_price_by_a_typed_number(h: Harness) -> None:
    await h.press(cb(TAR, "st", x="gb50"), user=ADMIN)
    assert "от 1 до 10000" in plain(h.last().text) and "Сейчас 50" in plain(h.last().text)
    await h.say("много", user=ADMIN)
    assert "Нужно целое число от 1 до 10000" in plain(h.last().text)
    await h.say("75", user=ADMIN)
    assert "Цена тарифа 50 ГБ: 75 Stars" in plain(h.last().text)
    assert h.catalog.get("gb50").stars == 75
    assert await h.fsm_state(ADMIN) is None


async def test_editing_volume_rejects_a_duplicate_and_applies_a_valid_one(h: Harness) -> None:
    await h.press(cb(TAR, "gb", x="gb50"), user=ADMIN)
    await h.say("100", user=ADMIN)
    assert T.TAR_DUPLICATE_GB in plain(h.last().text)
    await h.press(cb(TAR, "gb", x="gb50"), user=ADMIN)
    await h.say("60", user=ADMIN)
    assert "Объём тарифа 60 ГБ: 60 ГБ" in plain(h.last().text)
    assert h.catalog.get("gb50").gb == 60


async def test_hide_and_show_buttons(h: Harness) -> None:
    await h.press(cb(TAR, "vis", x="gb200", p=0), user=ADMIN)
    assert "скрыт" in plain(h.last().text) and not h.catalog.get("gb200").visible
    await h.press(cb(TAR, "vis", x="gb200", p=1), user=ADMIN)
    assert "снова продаётся" in plain(h.last().text) and h.catalog.get("gb200").visible
    assert [a for a, _ in await audit_actions(h)] == ["tariff.hide", "tariff.show"]


async def test_the_card_counts_clients_on_the_plan(h: Harness) -> None:
    await add_user(h.db, 8)
    await add_client(h.db, 8, 1)
    await add_user(h.db, 9)
    await add_client(h.db, 9, 1, state="deleted")
    await h.press(cb(TAR, "card", x="gb50"), user=ADMIN)
    assert "Клиентов на тарифе: 1" in plain(h.last().text)


@pytest.mark.parametrize(
    "data",
    [cb(TAR, "list"), cb(TAR, "card", x="gb50"), cb(TAR, "st", x="gb50"), cb(TAR, "gb", x="gb50"),
     cb(TAR, "rb", x="gb50"), cb(TAR, "vis", x="gb50", p=0)],
)  # fmt: skip
async def test_support_cannot_touch_tariffs(h: Harness, data: str) -> None:
    await h.press(data, user=SUPPORT)
    answer = h.session.of(AnswerCallbackQuery)[-1]
    assert (answer.text, answer.show_alert) == (T.ADM_DENIED, True)
    assert not await audit_actions(h)
    await h.say("/tariffs", user=SUPPORT)
    assert h.last().text == T.ADM_DENIED
    assert h.catalog.get("gb50").stars == 50 and h.catalog.get("gb50").visible


async def test_a_plan_name_with_markup_cannot_break_the_admin_screens(h: Harness) -> None:
    async with h.db.transaction() as conn:
        await plans_repo.update_fields(conn, "gb50", NOW, name="<b>Хитрый</b> & Ко")
    await h.plans.load()
    await h.say("/tariffs", user=ADMIN)
    assert "&lt;b&gt;Хитрый&lt;/b&gt; &amp; Ко" in h.last().text
    await h.press(cb(TAR, "card", x="gb50"), user=ADMIN)
    assert "&lt;b&gt;Хитрый" in h.last().text


def test_plan_service_is_exported() -> None:
    assert PlanService  # импорт нужен тестам стенда
