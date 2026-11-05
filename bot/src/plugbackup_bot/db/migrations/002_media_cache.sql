-- Кэш file_id баннеров: Telegram уже хранит файл, повторно загружать его не нужно.
-- Ключ — SHA-256 содержимого файла: если картинку в design/ заменили, она загрузится заново.
CREATE TABLE media_cache (
    sha256     TEXT PRIMARY KEY,
    file_id    TEXT NOT NULL,
    cached_at  INTEGER NOT NULL
) STRICT;
