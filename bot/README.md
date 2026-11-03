# Бот PlugBackup

Telegram-бот для оплаты и выдачи доступа. Единственное место, где клиент регистрируется, платит и получает логин для SFTP. Написан на Python 3.12, aiogram 3 и SQLite.

Что умеет:

- Бета по инвайт-коду (10 ГБ) и платные тарифы в Telegram Stars с автопродлением раз в 30 дней.
- Выдача доступа: проверка SSH-ключа ed25519, выбор ноды с наибольшим запасом, готовая инструкция для restic.
- `/status`, `/upgrade`, `/key`, `/alerts`, `/restore`, `/rules`, `/help`.
- Уведомления об окончании подписки, заполнении квоты, блокировке и удалении. Алерты безопасности настраиваются клиентом в `/alerts`.
- Админка: `/stats`, `/invite`, `/client`, `/broadcast`, `/nodes`, `/maint`. Ошибки `storectl`, недоступные ноды и сбои платежей приходят в админ-чат.

С нодой бот общается по SSH: вызывает `storectl` (см. [node/](../node)).

## Что понадобится

- VPS с Linux, Python 3.12+, SQLite 3.37+ и клиентом `ssh`.
- WireGuard-туннель до ноды: бот ходит на её адрес в туннеле.
- Нода, настроенная по [node/README.md](../node/README.md).
- Бот в BotFather и его токен. Для платежей в Stars провайдер платежей не нужен.

## Установка на VPS

Команды выполняются от root. Пути можно менять, тогда поправьте юнит.

**1. Пользователь, код, окружение.**

```bash
useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin plugbackup-bot
install -d -m 0750 -o root -g plugbackup-bot /etc/plugbackup-bot
git clone <адрес репозитория> /opt/plugbackup
python3.12 -m venv /opt/plugbackup/bot/.venv
/opt/plugbackup/bot/.venv/bin/pip install /opt/plugbackup/bot
```

**2. Ключ для ноды.** Бот входит на ноду как пользователь `provision`. Ключ без пароля: бот работает без участия человека.

```bash
ssh-keygen -t ed25519 -N '' -C plugbackup-bot -f /etc/plugbackup-bot/provision_ed25519
chown plugbackup-bot:plugbackup-bot /etc/plugbackup-bot/provision_ed25519
chmod 0600 /etc/plugbackup-bot/provision_ed25519
```

Публичную часть (`provision_ed25519.pub`) положите на ноду по [инструкции](../node/README.md), шаг 4.

**3. Отпечаток ноды.** Бот подключается с `StrictHostKeyChecking=yes`, поэтому ключ ноды нужен заранее.

```bash
ssh-keyscan -t ed25519 <адрес ноды в WireGuard> > /etc/plugbackup-bot/known_hosts
ssh-keygen -lf /etc/plugbackup-bot/known_hosts
```

Сверьте отпечаток с тем, что покажет `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` на самой ноде. Без сверки `ssh-keyscan` доверяет первому ответу.

**4. Настройки.**

```bash
install -m 0640 -o root -g plugbackup-bot /opt/plugbackup/bot/.env.example /etc/plugbackup-bot/.env
$EDITOR /etc/plugbackup-bot/.env
```

| Переменная | Что это |
| --- | --- |
| `BOT_TOKEN` | Токен от BotFather |
| `ADMIN_IDS` | Telegram id администраторов через запятую |
| `ADMIN_CHAT_ID` | Куда слать алерты. Бот должен уже получить от этого чата `/start`, иначе писать первым он не может |
| `DB_PATH` | Файл базы, например `/var/lib/plugbackup-bot/bot.db` |
| `PLANS_FILE` | Абсолютный путь к `design/data/plans.json`, например `/opt/plugbackup/design/data/plans.json` |
| `PRICES_STARS` | Цена каждого платного тарифа в Stars за 30 дней, от 1 до 10000 |
| `NODES` | Список нод в JSON: имя, адрес в WireGuard, публичный хост, ёмкость в ГБ, коэффициент перепродажи |
| `SSH_USER`, `SSH_PORT`, `SSH_KEY_PATH`, `SSH_KNOWN_HOSTS_PATH` | Подключение к ноде |
| `STORECTL_TIMEOUT_S` | Сколько ждать ответа `storectl` |
| `DEFAULT_TZ` | Часовой пояс по умолчанию для тихих часов |
| `LOG_LEVEL` | `INFO` или `DEBUG` |

Названия и объёмы тарифов берутся из `design/data/plans.json`, цены в Stars только отсюда. Значения в `.env.example` — заглушки, цены подберите по текущему курсу Stars. Ноды задаются здесь же и применяются при запуске. Нода, убранная из `NODES`, отключается (новые клиенты на неё не идут), но не удаляется.

**5. Проверка и запуск.**

```bash
cd /etc/plugbackup-bot && sudo -u plugbackup-bot /opt/plugbackup/bot/.venv/bin/python -m plugbackup_bot --check
install -m 0644 /opt/plugbackup/bot/deploy/plugbackup-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now plugbackup-bot
journalctl -u plugbackup-bot -f
```

`--check` проверяет настройки, наличие ключа и `known_hosts`, читает тарифы и создаёт базу. Права на файл ключа должны быть `0600`. После старта администратор получает сообщение «Бот запущен».

Юнит написан без проверки на живой системе. Если сервис не стартует, смотрите журнал и ослабляйте строки защиты по одной.

## Резервная копия базы

В базе лежат Telegram id клиентов, их логины и история платежей. Потеря базы означает потерю привязки клиентов к хранилищам.

Разовая копия, безопасная при работающем боте:

```bash
sudo -u plugbackup-bot sqlite3 /var/lib/plugbackup-bot/bot.db ".backup '/var/lib/plugbackup-bot/backups/bot-$(date +%F).db'"
```

Ежедневно и с проверкой целостности — скрипт и таймер:

```bash
install -m 0644 /opt/plugbackup/bot/deploy/plugbackup-bot-backup.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now plugbackup-bot-backup.timer
```

Копии лежат в `/var/lib/plugbackup-bot/backups`, хранятся 14 дней. Это та же машина: настройте вывоз копий куда-нибудь ещё и шифруйте их. Восстановление: остановить бота, положить копию на место `bot.db`, запустить бота.

## Проверка на тестовом клиенте

Пройдите это один раз до приглашения людей.

1. `--check` проходит, сервис работает, админ получил «Бот запущен».
2. Администратором: `/nodes` показывает ноду, `/invite 1` выдаёт код.
3. Другим аккаунтом: `/start`, «Бета по инвайту», «Я понимаю риски», код, ключ ed25519. Должно прийти сообщение «Доступ готов» с хостом и логином.
4. На ноде: `storectl usage u1001` отвечает двумя числами. С клиента: `sftp -i <ключ> -o IdentitiesOnly=yes u1001@<хост>` пускает, `ls` показывает только `data`.
5. restic: `restic -r sftp:plugbackup:/data/restic init` и небольшая копия. Через час (или после рестарта бота) `/status` показывает занятое место.
6. `/alerts`: включить «Заполнено на 80%», сменить порог, выключить. Включить «Новый IP»: экран объясняет, что бот получает только хеши.
7. Платная подписка. Временно поставьте в `PRICES_STARS` цену 1 Star, оформите тариф со второго аккаунта. Убедитесь, что пришёл «Доступ готов», а в `/client u1002` видна оплата. Отключите продление в `/status` и проверьте сообщение. Верните Stars вызовом `refundStarPayment` (в бот он не вынесен), затем верните нормальные цены.
8. `/restore`: заявка приходит в админ-чат, кнопка «Выполнено» отвечает клиенту.
9. `/client u1001` → «Заблокировать» → попытка входа по SFTP отклоняется → «Вернуть доступ».

Пока не проверено на реальных Stars, и первый платёж надо наблюдать по журналу:

- Не приходит ли `pre_checkout_query` на автопродление. Бот отклоняет повторную оплату нового доступа, и если Telegram вдруг шлёт такой запрос при продлении, продление не пройдёт.
- Принимает ли `editUserStarSubscription` идентификатор платежа первой оплаты. Он хранится в `clients.sub_charge_id`. Если нет, отключение продления из бота и после смены тарифа будет падать с алертом админу.
- Получает ли бот новый `telegram_payment_charge_id` при каждом продлении.

## Обновление

```bash
cd /opt/plugbackup && git pull
/opt/plugbackup/bot/.venv/bin/pip install --upgrade /opt/plugbackup/bot
systemctl restart plugbackup-bot
```

Миграции базы применяются при старте и идут только вперёд. Перед обновлением сделайте копию базы. Бот отказывается запускаться, если схема базы новее кода.

## Как это устроено

Деньги идут по пути: счёт-ссылка (`createInvoiceLink`, подписка на 30 дней в Stars) → `pre_checkout_query` → `successful_payment` → запись в БД в одной транзакции → задача для `storectl` в очереди. Платёж идемпотентен по `telegram_payment_charge_id`. Если `storectl` упал, задача остаётся в очереди и повторяется (1, 2, 5, 15, 30, 60 минут, дальше раз в час), админ получает алерт.

Состояние клиента: `pending` → `active` → `expiring` → `suspended` → `deleted`. Менять его можно только функцией `transition` в `db/clients.py`, она же пишет журнал переходов.

| Слой | Где |
| --- | --- |
| Правила без ввода-вывода: ключи, тарифы, состояния, жизненный цикл, алерты | `src/plugbackup_bot/domain/` |
| SQLite: схема, репозитории | `src/plugbackup_bot/db/` |
| Логика: платежи, выдача доступа, очередь `storectl`, алерты, админка | `src/plugbackup_bot/services/` |
| `storectl` по SSH, часы | `src/plugbackup_bot/infra/` |
| Telegram: handlers, тексты, клавиатуры | `src/plugbackup_bot/tg/` |
| Планировщик, сборка, запуск | `scheduler.py`, `runtime.py`, `__main__.py` |

## Разработка

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                    # бот
.venv/bin/pytest ../node/tests      # storectl
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

На Windows интерпретатор лежит в `.venv\Scripts\`.

Тесты не ходят в сеть: Telegram подменён, `storectl` замокан, часы управляются из теста. Контракт между ботом и нодой проверяет `tests/test_node_contract.py`.
