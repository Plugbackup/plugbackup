"""Тексты админки, которые ещё не перенесены в разделы: список нод и инвайт-коды."""

from collections.abc import Sequence

from plugbackup_bot.services.admin import NodeLoad
from plugbackup_bot.tg.format import NBSP, fmt_datetime
from plugbackup_bot.tg.texts import esc


def nodes(loads: Sequence[NodeLoad], tz: str) -> str:
    lines = ["Ноды", ""]
    for load in loads:
        node = load.node
        lines.append(
            f"• {esc(node.name)} — {esc(node.addr)} → {esc(node.public_host)}\n"
            f"  ёмкость {node.capacity_gb}{NBSP}ГБ, коэффициент {node.oversell:g}, "
            f"лимит {node.limit_gb}{NBSP}ГБ, продано {load.sold_gb}{NBSP}ГБ"
            + ("" if node.enabled else ", отключена")
            + (f", недоступна с {fmt_datetime(node.down_since, tz)}" if node.down_since else "")
        )
    lines += ["", "Список нод задаётся в .env (NODES) и применяется при перезапуске бота."]
    return "\n".join(lines)


def invites(codes: Sequence[str]) -> str:
    body = "\n".join(f"<code>{esc(code)}</code>" for code in codes)
    return f"Инвайт-коды ({len(codes)}), каждый действует один раз:\n\n{body}"
