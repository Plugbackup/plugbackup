-- Рассылки: сегмент, кнопки, отложенная отправка и список получателей, чтобы незаконченная
-- рассылка продолжилась после перезапуска и по ней была статистика.
ALTER TABLE broadcasts ADD COLUMN segment TEXT NOT NULL DEFAULT 'all';
ALTER TABLE broadcasts ADD COLUMN buttons_json TEXT NOT NULL DEFAULT '[]';
-- Старые рассылки отправлялись сразу и уже завершены.
ALTER TABLE broadcasts ADD COLUMN status TEXT NOT NULL DEFAULT 'done'
    CHECK (status IN ('scheduled', 'sending', 'done', 'cancelled'));
ALTER TABLE broadcasts ADD COLUMN send_at INTEGER;
ALTER TABLE broadcasts ADD COLUMN total INTEGER NOT NULL DEFAULT 0;
ALTER TABLE broadcasts ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0;
ALTER TABLE broadcasts ADD COLUMN started_at INTEGER;
ALTER TABLE broadcasts ADD COLUMN finished_at INTEGER;
-- manual: от администратора, maintenance: о начале и конце техработ.
ALTER TABLE broadcasts ADD COLUMN kind TEXT NOT NULL DEFAULT 'manual';
CREATE INDEX broadcasts_by_status ON broadcasts (status, send_at);

CREATE TABLE broadcast_recipients (
    broadcast_id  INTEGER NOT NULL REFERENCES broadcasts (id),
    tg_id         INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'sent', 'blocked', 'failed')),
    sent_at       INTEGER,
    PRIMARY KEY (broadcast_id, tg_id)
) STRICT, WITHOUT ROWID;
CREATE INDEX broadcast_recipients_by_status ON broadcast_recipients (broadcast_id, status);
