-- Кто обработал заявку на восстановление: tg_id администратора.
ALTER TABLE restore_requests ADD COLUMN handled_by INTEGER;
