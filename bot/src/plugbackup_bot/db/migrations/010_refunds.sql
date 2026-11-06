-- Возврат Stars из админки: когда, кто вернул и отключили ли при этом доступ.
ALTER TABLE payments ADD COLUMN refunded_at INTEGER;
ALTER TABLE payments ADD COLUMN refunded_by INTEGER;
ALTER TABLE payments ADD COLUMN refund_revoked INTEGER NOT NULL DEFAULT 0
    CHECK (refund_revoked IN (0, 1));
