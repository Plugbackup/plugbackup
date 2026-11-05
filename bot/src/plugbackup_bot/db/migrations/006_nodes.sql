-- Режим обслуживания ноды (новые клиенты не выдаются) и источник записи: ноды из .env
-- синхронизируются при старте, ноды, добавленные из бота, остаются как есть.
ALTER TABLE nodes ADD COLUMN maintenance INTEGER NOT NULL DEFAULT 0 CHECK (maintenance IN (0, 1));
ALTER TABLE nodes ADD COLUMN source TEXT NOT NULL DEFAULT 'env' CHECK (source IN ('env', 'bot'));
