-- Kapanan parçanın kapanış anındaki TP kademeleri ve SL'si. Çıkış karnesi
-- değmeyen en yakın TP'yi ve en kötü fiyatın SL'ye uzaklığını bunlarla ölçer.
ALTER TABLE trades ADD COLUMN tp_levels TEXT NOT NULL DEFAULT '[]';
ALTER TABLE trades ADD COLUMN sl REAL;
