-- Обращения в поддержку. У человека не больше одного незакрытого обращения: новые сообщения
-- дополняют его, пока администратор или сам клиент его не закроет.
CREATE TABLE tickets (
    id          INTEGER PRIMARY KEY,
    tg_id       INTEGER NOT NULL REFERENCES users (tg_id),
    -- open: ждёт ответа поддержки, answered: поддержка ответила, closed: закрыто.
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'answered', 'closed')),
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    closed_at   INTEGER,
    -- Кто закрыл: tg_id администратора или NULL, если закрыл сам клиент.
    closed_by   INTEGER
) STRICT;
CREATE UNIQUE INDEX tickets_one_open_per_user ON tickets (tg_id) WHERE status <> 'closed';
CREATE INDEX tickets_by_status ON tickets (status, updated_at);

CREATE TABLE ticket_messages (
    id         INTEGER PRIMARY KEY,
    ticket_id  INTEGER NOT NULL REFERENCES tickets (id),
    author     TEXT NOT NULL CHECK (author IN ('client', 'admin')),
    admin_id   INTEGER,
    text       TEXT NOT NULL,
    at         INTEGER NOT NULL
) STRICT;
CREATE INDEX ticket_messages_by_ticket ON ticket_messages (ticket_id, id);
