-- Роли администраторов. Владельцы из ADMIN_IDS попадают сюда при каждом старте (source = 'env'),
-- помощников добавляет владелец из бота (source = 'bot').
CREATE TABLE admins (
    tg_id     INTEGER PRIMARY KEY,
    role      TEXT NOT NULL CHECK (role IN ('owner', 'support')),
    source    TEXT NOT NULL CHECK (source IN ('env', 'bot')),
    added_by  INTEGER,
    added_at  INTEGER NOT NULL
) STRICT;

-- Журнал действий администраторов: кто, что, когда и над кем. Тексты сообщений сюда не пишутся.
CREATE TABLE audit_log (
    id           INTEGER PRIMARY KEY,
    at           INTEGER NOT NULL,
    actor_id     INTEGER NOT NULL,
    actor_role   TEXT NOT NULL,
    action       TEXT NOT NULL,
    target_type  TEXT,
    target_id    TEXT,
    details      TEXT NOT NULL DEFAULT ''
) STRICT;
CREATE INDEX audit_by_time ON audit_log (at);
CREATE INDEX audit_by_actor ON audit_log (actor_id, at);
CREATE INDEX audit_by_target ON audit_log (target_type, target_id);

-- Для поиска клиента по @username. Заполняется, когда человек пишет боту.
ALTER TABLE users ADD COLUMN username TEXT;
ALTER TABLE users ADD COLUMN last_seen_at INTEGER;
CREATE INDEX users_by_username ON users (username);

-- Заметка администратора и бонусные ГБ сверх тарифа: квота клиента = объём тарифа + bonus_gb.
ALTER TABLE clients ADD COLUMN admin_note TEXT;
ALTER TABLE clients ADD COLUMN bonus_gb INTEGER NOT NULL DEFAULT 0 CHECK (bonus_gb >= 0);

-- Настройки, которые меняются на ходу: режим техработ и подобное.
CREATE TABLE app_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  INTEGER NOT NULL
) STRICT, WITHOUT ROWID;
