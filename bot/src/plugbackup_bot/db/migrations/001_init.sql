-- Схема бота PlugBackup.
-- Время везде unix-секунды UTC. Булевы значения: 0 или 1.
-- Строки clients не удаляются: логин u{1000+id} не должен использоваться повторно.

CREATE TABLE users (
    tg_id          INTEGER PRIMARY KEY,
    first_seen_at  INTEGER NOT NULL,
    beta_used_at   INTEGER,
    bot_blocked    INTEGER NOT NULL DEFAULT 0 CHECK (bot_blocked IN (0, 1))
) STRICT;

-- Согласие с рисками беты: что показывали (версия текста) и когда клиент согласился.
CREATE TABLE risk_acks (
    id            INTEGER PRIMARY KEY,
    tg_id         INTEGER NOT NULL REFERENCES users (tg_id),
    text_version  INTEGER NOT NULL,
    acked_at      INTEGER NOT NULL
) STRICT;
CREATE INDEX risk_acks_by_user ON risk_acks (tg_id, text_version);

-- Инвайт-коды хранятся только в виде хеша.
CREATE TABLE invites (
    code_hash   TEXT PRIMARY KEY,
    created_by  INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,
    used_by     INTEGER REFERENCES users (tg_id),
    used_at     INTEGER
) STRICT;

-- Синхронизируется с NODES из .env при старте.
CREATE TABLE nodes (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    addr         TEXT NOT NULL,
    public_host  TEXT NOT NULL,
    capacity_gb  INTEGER NOT NULL CHECK (capacity_gb >= 0),
    oversell     REAL NOT NULL DEFAULT 1.0 CHECK (oversell > 0),
    enabled      INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    down_since   INTEGER
) STRICT;

CREATE TABLE clients (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id           INTEGER NOT NULL REFERENCES users (tg_id),
    node_id         INTEGER NOT NULL REFERENCES nodes (id),
    plan_id         TEXT NOT NULL,
    quota_gb        INTEGER NOT NULL CHECK (quota_gb > 0),
    state           TEXT NOT NULL
                    CHECK (state IN ('pending', 'active', 'expiring', 'suspended', 'deleted')),
    ssh_key_body    TEXT,
    -- Конец оплаченного периода (платные) или срок следующего «Я ещё пользуюсь» (бета).
    expires_at      INTEGER,
    confirm_due_at  INTEGER,
    state_since     INTEGER NOT NULL,
    -- telegram_payment_charge_id действующей подписки Stars, нужен для отмены продления.
    sub_charge_id   TEXT,
    -- invoice_payload той же подписки: по нему продления и апдейты подписки находят клиента.
    sub_payload     TEXT,
    renewal_on      INTEGER NOT NULL DEFAULT 1 CHECK (renewal_on IN (0, 1)),
    -- Ручная блокировка админом: автоматика не возвращает доступ.
    admin_hold      INTEGER NOT NULL DEFAULT 0 CHECK (admin_hold IN (0, 1)),
    created_at      INTEGER NOT NULL
) STRICT;
-- Не больше одного живого клиента на Telegram-аккаунт.
CREATE UNIQUE INDEX clients_one_live_per_user ON clients (tg_id) WHERE state <> 'deleted';
CREATE INDEX clients_by_state ON clients (state);
CREATE INDEX clients_by_node ON clients (node_id);

CREATE TABLE state_log (
    id          INTEGER PRIMARY KEY,
    client_id   INTEGER NOT NULL REFERENCES clients (id),
    from_state  TEXT,
    to_state    TEXT NOT NULL,
    reason      TEXT NOT NULL,
    actor       TEXT NOT NULL,
    at          INTEGER NOT NULL
) STRICT;
CREATE INDEX state_log_by_client ON state_log (client_id, at);

CREATE TABLE payments (
    id                          INTEGER PRIMARY KEY,
    tg_id                       INTEGER NOT NULL REFERENCES users (tg_id),
    client_id                   INTEGER REFERENCES clients (id),
    plan_id                     TEXT NOT NULL,
    stars                       INTEGER NOT NULL CHECK (stars > 0),
    -- Уникальность даёт идемпотентность: повторная доставка платежа не создаёт второго клиента.
    telegram_payment_charge_id  TEXT NOT NULL UNIQUE,
    provider_payment_charge_id  TEXT,
    invoice_payload             TEXT NOT NULL DEFAULT '',
    is_recurring               INTEGER NOT NULL DEFAULT 0 CHECK (is_recurring IN (0, 1)),
    is_first                    INTEGER NOT NULL DEFAULT 0 CHECK (is_first IN (0, 1)),
    sub_expires_at              INTEGER,
    status                      TEXT NOT NULL DEFAULT 'paid' CHECK (status IN ('paid', 'refunded')),
    at                          INTEGER NOT NULL
) STRICT;
CREATE INDEX payments_by_user ON payments (tg_id);
CREATE INDEX payments_by_time ON payments (at);

-- Очередь операций storectl. Упавшая операция остаётся здесь и повторяется.
CREATE TABLE provision_jobs (
    id               INTEGER PRIMARY KEY,
    client_id        INTEGER NOT NULL REFERENCES clients (id),
    op               TEXT NOT NULL CHECK (op IN ('add', 'resize', 'suspend', 'resume', 'delete')),
    params_json      TEXT NOT NULL DEFAULT '{}',
    -- failed: повторы исчерпаны или ошибка не лечится повтором, нужен админ.
    status           TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'failed')),
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at  INTEGER NOT NULL,
    last_error       TEXT,
    created_at       INTEGER NOT NULL,
    done_at          INTEGER
) STRICT;
CREATE INDEX provision_jobs_due ON provision_jobs (status, next_attempt_at);
CREATE INDEX provision_jobs_by_client ON provision_jobs (client_id);

-- Занятое место по часам, хранится 30 дней.
CREATE TABLE usage_samples (
    client_id   INTEGER NOT NULL REFERENCES clients (id),
    hour_ts     INTEGER NOT NULL,
    used_bytes  INTEGER NOT NULL,
    quota_bytes INTEGER NOT NULL,
    PRIMARY KEY (client_id, hour_ts)
) STRICT, WITHOUT ROWID;

-- Неудачные входы по часам (алерт 5). IP здесь нет.
CREATE TABLE login_fail_hourly (
    client_id  INTEGER NOT NULL REFERENCES clients (id),
    hour_ts    INTEGER NOT NULL,
    fails      INTEGER NOT NULL,
    PRIMARY KEY (client_id, hour_ts)
) STRICT, WITHOUT ROWID;

-- HMAC-хеши IP от ноды (алерт 6): не больше 20 на клиента, не дольше 30 дней.
CREATE TABLE ip_hashes (
    client_id    INTEGER NOT NULL REFERENCES clients (id),
    ip_hash      TEXT NOT NULL,
    first_seen   INTEGER NOT NULL,
    last_seen    INTEGER NOT NULL,
    notified_at  INTEGER,
    PRIMARY KEY (client_id, ip_hash)
) STRICT, WITHOUT ROWID;

-- Общие настройки алертов клиента. tz = NULL означает DEFAULT_TZ из конфига.
CREATE TABLE alert_prefs (
    tg_id           INTEGER PRIMARY KEY REFERENCES users (tg_id),
    tz              TEXT,
    quiet_from      INTEGER NOT NULL DEFAULT 23 CHECK (quiet_from BETWEEN 0 AND 23),
    quiet_to        INTEGER NOT NULL DEFAULT 8 CHECK (quiet_to BETWEEN 0 AND 23),
    cooldown_hours  INTEGER NOT NULL DEFAULT 24 CHECK (cooldown_hours > 0)
) STRICT;

-- Настройки отдельных алертов. Набор ключей определяется кодом, не схемой.
CREATE TABLE alert_settings (
    tg_id       INTEGER NOT NULL REFERENCES users (tg_id),
    alert_key   TEXT NOT NULL,
    enabled     INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    threshold   INTEGER,
    consent_at  INTEGER,
    PRIMARY KEY (tg_id, alert_key)
) STRICT, WITHOUT ROWID;

-- Отправленные уведомления: кулдаун алертов и защита от повторов после перезапуска.
CREATE TABLE notifications (
    id          INTEGER PRIMARY KEY,
    client_id   INTEGER NOT NULL REFERENCES clients (id),
    kind        TEXT NOT NULL,
    dedupe_key  TEXT NOT NULL DEFAULT '',
    sent_at     INTEGER NOT NULL,
    UNIQUE (client_id, kind, dedupe_key)
) STRICT;
CREATE INDEX notifications_by_kind ON notifications (client_id, kind, sent_at);

CREATE TABLE service_events (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('maintenance', 'restore', 'node_down')),
    node_id      INTEGER REFERENCES nodes (id),
    text         TEXT NOT NULL,
    starts_at    INTEGER NOT NULL,
    ends_at      INTEGER,
    notified_at  INTEGER
) STRICT;

CREATE TABLE restore_requests (
    id             INTEGER PRIMARY KEY,
    client_id      INTEGER NOT NULL REFERENCES clients (id),
    snapshot_date  TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'done', 'rejected')),
    note           TEXT,
    created_at     INTEGER NOT NULL,
    handled_at     INTEGER
) STRICT;

CREATE TABLE broadcasts (
    id          INTEGER PRIMARY KEY,
    text        TEXT NOT NULL,
    created_by  INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,
    sent        INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0
) STRICT;
