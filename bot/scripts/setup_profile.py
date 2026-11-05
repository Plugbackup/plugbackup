"""Настраивает профиль бота: аватар, имя, описания и меню команд.

Запуск из папки bot/ (там лежит .env):

    python scripts/setup_profile.py --dry-run   # показать, ничего не меняя
    python scripts/setup_profile.py             # применить после подтверждения
"""

import sys

from plugbackup_bot.profile import main

if __name__ == "__main__":
    sys.exit(main())
