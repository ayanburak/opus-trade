-- Planlı çağrı müdahalesi kalktı, yönetici çağrı saatini mesajın içinde
-- söyler. Onun zamanını ve uyandırma anını tutan sütunlar silinir.
ALTER TABLE interventions DROP COLUMN due_at;
ALTER TABLE interventions DROP COLUMN fired_at;
