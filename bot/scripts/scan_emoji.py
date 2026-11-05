"""Сохраняет пак эмодзи из Telegram в emoji_pack.json и подбирает emoji_map.json.

Запуск из папки bot/ (там лежит .env):

    python scripts/scan_emoji.py
    python scripts/scan_emoji.py --remap   # заново подобрать всё, затирая ручные правки
"""

import sys

from plugbackup_bot.emoji_tools import main

if __name__ == "__main__":
    sys.exit(main())
