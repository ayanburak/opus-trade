-- SL emri mark fiyatı yerine son fiyatla tetiklenebilir. Var olan emirler
-- mark fiyatıyla tetiklenmeye devam eder.
ALTER TABLE orders ADD COLUMN by_last INTEGER NOT NULL DEFAULT 0;
