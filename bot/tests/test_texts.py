"""У каждого исхода сценария есть текст для клиента."""

from plugbackup_bot.domain.keys import KeyProblem
from plugbackup_bot.domain.lifecycle import Notice, NoticeKind
from plugbackup_bot.services.access import RedeemKind
from plugbackup_bot.services.billing import Reject
from plugbackup_bot.tg import texts
from plugbackup_bot.tg.notices import TelegramNoticeRenderer


def test_every_rejection_is_explained() -> None:
    assert set(texts.REJECTIONS) == set(Reject)
    assert all(text.strip() for text in texts.REJECTIONS.values())


def test_every_invite_failure_is_explained() -> None:
    assert set(texts.INVITE_ERRORS) == set(RedeemKind) - {RedeemKind.OK}


def test_every_key_problem_is_explained() -> None:
    assert set(texts.KEY_PROBLEMS) == set(KeyProblem)


def test_every_notice_renders_for_beta_and_paid() -> None:
    from helpers import NOW
    from plugbackup_bot.db.clients import Client
    from plugbackup_bot.domain.states import State

    client = Client(
        id=1, tg_id=5, node_id=1, plan_id="gb50", quota_gb=50, state=State.ACTIVE,
        ssh_key_body=None, expires_at=None, confirm_due_at=None, state_since=NOW,
        sub_charge_id=None, sub_payload=None, renewal_on=True, admin_hold=False, created_at=NOW,
    )  # fmt: skip
    renderer = TelegramNoticeRenderer("Europe/Moscow")
    for kind in NoticeKind:
        for beta in (True, False):
            text, _ = renderer.render(Notice(kind, "k", beta, at=NOW), client)
            assert text.strip(), (kind, beta)
            assert "!" not in text  # тон из design/brand.md: без восклицательных знаков


def test_texts_follow_the_brand_rules() -> None:
    samples = [texts.HELP, texts.rules(), texts.HOW_IT_WORKS, texts.RISK_TEXT, texts.KEY_HELP]
    samples += list(texts.REJECTIONS.values()) + list(texts.KEY_PROBLEMS.values())
    for text in samples:
        assert "!" not in text
        assert not any(ord(ch) > 0x1F000 for ch in text)  # эмодзи нет
