-- Toplayıcının ilk şeması. Her tabloda tekrar eden kayıt yazılamaz, böylece
-- geriye dönük doldurma ya da eski verinin aktarımı iki kez çalışsa da veri
-- ikilenmez. Zamanlar unix saniyedir.

-- Bütün piyasanın likidasyonları. Binance saniyede parite başına yalnızca en
-- büyük likidasyonu gönderir, dolar karşılığı fiyat çarpı miktardır.
CREATE TABLE liquidations (
    time    INTEGER NOT NULL,
    symbol  TEXT NOT NULL,
    side    TEXT NOT NULL,
    price   REAL NOT NULL,
    qty     REAL NOT NULL,
    UNIQUE (symbol, time, side, price, qty)
);
CREATE INDEX liquidations_time ON liquidations (time);

-- Dakikalık mark fiyatı, endeks ve tahmini fonlama. Tahmini fonlamanın
-- geçmişi Binance'te yoktur.
CREATE TABLE premium (
    symbol        TEXT NOT NULL,
    time          INTEGER NOT NULL,
    mark          REAL NOT NULL,
    index_price   REAL NOT NULL,
    est_funding   REAL NOT NULL,
    next_funding  INTEGER NOT NULL,
    PRIMARY KEY (symbol, time)
) WITHOUT ROWID;

-- Beş dakikalık istatistikler. Binance bunların yalnızca son otuz gününü verir.
CREATE TABLE open_interest (
    symbol  TEXT NOT NULL,
    time    INTEGER NOT NULL,
    qty     REAL NOT NULL,
    usd     REAL,
    PRIMARY KEY (symbol, time)
) WITHOUT ROWID;

CREATE TABLE long_short (
    kind      TEXT NOT NULL,
    symbol    TEXT NOT NULL,
    time      INTEGER NOT NULL,
    long_pct  REAL NOT NULL,
    ratio     REAL NOT NULL,
    PRIMARY KEY (kind, symbol, time)
) WITHOUT ROWID;

CREATE TABLE taker_volume (
    symbol          TEXT NOT NULL,
    time            INTEGER NOT NULL,
    buy_sell_ratio  REAL NOT NULL,
    buy_volume      REAL NOT NULL,
    sell_volume     REAL NOT NULL,
    PRIMARY KEY (symbol, time)
) WITHOUT ROWID;

-- Yalnızca canlıdan alınabilen kaynakların kesintisiz çalıştığı aralıklar.
-- İki satırın arası verinin hiç olmadığı boşluktur.
CREATE TABLE coverage (
    source      TEXT NOT NULL,
    start_time  INTEGER NOT NULL,
    end_time    INTEGER NOT NULL
);
CREATE INDEX coverage_source ON coverage (source, start_time);

-- Premium ve istatistikleri toplanan pariteler. Likidasyonlar bu listeden
-- bağımsız olarak bütün piyasadan toplanır.
CREATE TABLE tracked_symbols (
    symbol    TEXT PRIMARY KEY,
    added_at  INTEGER NOT NULL
);

INSERT INTO tracked_symbols (symbol, added_at)
SELECT column1, CAST(strftime('%s') AS INTEGER) FROM (VALUES
    ('BTCUSDT'), ('ETHUSDT'), ('SOLUSDT'), ('XRPUSDT'), ('ZECUSDT'), ('BNBUSDT'), ('DOGEUSDT'), ('ADAUSDT'),
    ('AVAXUSDT'), ('LINKUSDT'), ('LTCUSDT'), ('DOTUSDT'), ('BCHUSDT'), ('TRXUSDT'), ('NEARUSDT'), ('ATOMUSDT'),
    ('FILUSDT'), ('UNIUSDT'), ('AAVEUSDT'), ('ETCUSDT'), ('XAUUSDT')
);
