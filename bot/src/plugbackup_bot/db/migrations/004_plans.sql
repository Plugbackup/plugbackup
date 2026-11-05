-- Тарифы. Заполняются при первом запуске из design/data/plans.json и PRICES_STARS, дальше источник
-- правды эта таблица: админ меняет цену и объём из бота, без перезапуска. Цена и объём действуют
-- на новые покупки, у действующих подписок и клиентов остаются прежними (clients.quota_gb).
CREATE TABLE plans (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    gb          INTEGER NOT NULL CHECK (gb > 0),
    price_rub   INTEGER NOT NULL CHECK (price_rub >= 0),
    stars       INTEGER NOT NULL CHECK (stars >= 0),
    visible     INTEGER NOT NULL DEFAULT 1 CHECK (visible IN (0, 1)),
    sort        INTEGER NOT NULL DEFAULT 0,
    updated_at  INTEGER NOT NULL
) STRICT;
