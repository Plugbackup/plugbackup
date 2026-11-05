-- Инвайты с лимитом использований, сроком и отзывом. Старые коды остаются одноразовыми.
ALTER TABLE invites ADD COLUMN max_uses INTEGER NOT NULL DEFAULT 1 CHECK (max_uses >= 1);
ALTER TABLE invites ADD COLUMN uses INTEGER NOT NULL DEFAULT 0 CHECK (uses >= 0);
ALTER TABLE invites ADD COLUMN expires_at INTEGER;
ALTER TABLE invites ADD COLUMN revoked_at INTEGER;
-- Последние три знака кода: по ним админ узнаёт код в списке, сам код в базе не хранится.
ALTER TABLE invites ADD COLUMN code_tail TEXT;

-- Кто и когда воспользовался кодом.
CREATE TABLE invite_uses (
    code_hash  TEXT NOT NULL REFERENCES invites (code_hash),
    tg_id      INTEGER NOT NULL REFERENCES users (tg_id),
    used_at    INTEGER NOT NULL,
    PRIMARY KEY (code_hash, tg_id)
) STRICT, WITHOUT ROWID;
CREATE INDEX invite_uses_by_user ON invite_uses (tg_id);

UPDATE invites SET uses = 1 WHERE used_by IS NOT NULL;
INSERT INTO invite_uses (code_hash, tg_id, used_at)
    SELECT code_hash, used_by, COALESCE(used_at, created_at) FROM invites WHERE used_by IS NOT NULL;
