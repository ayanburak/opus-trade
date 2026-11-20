"""
file: api.py
importable: create_collector_app

Toplayıcının küçük HTTP uygulaması. create_collector_app() toplayıcının
deposunu, market.db'nin yolunu, Binance'e giden piyasa istemcisini ve saati
alır. Uçlar:

    GET  /health           ulaşılabilirlik ve izlenen parite sayısı
    GET  /summary          kapsama raporu, report.summary()
    POST /symbols          {"symbol": "BTCUSDT"} izlenenlere ekler
    POST /symbols/remove   {"symbol": "BTCUSDT"} izlenenlerden çıkarır
    GET  /export           table, format, symbol, kind, start, end ile indirme

Uygulama yalnızca toplayıcının Unix soketinde dinler. Admin paneli ona
opus-web üzerinden ulaşır.

description: Veri sayfasının kullandığı toplayıcı uçları.
"""

# NOTE: tracked_symbols'a yalnızca toplayıcı yazar, panelin ekleme ve çıkarma
# isteği de toplayıcının kendi bağlantısından geçer. Kaynaklar aynı olay
# döngüsünde çalıştığı için yazılar birbirine karışmaz. Okuma uçları her
# istekte ayrı, salt okunur bir bağlantı açar.
#
# NOTE: Eklenen parite önce Binance'e sorulur. Yanlış yazılmış bir parite
# izlenseydi istatistik kaynağı her beş dakikada bir uyarı yazardı.
# Çıkarılan paritenin verisi silinmez, yalnızca toplanması durur.
#
# NOTE: Hatalar Türkçe mesajla 400 döner, izlenmeyen bir paritenin çıkarılması
# 404'tür. Panel mesajı olduğu gibi gösterir.

import asyncio
import re
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..domain.clock import Clock
from ..market.source import MarketData, MarketDataError
from ..storage.database import connect
from .report import plan_export, render, summary
from .store import CollectorStore

_SYMBOL = re.compile(r"^[A-Z0-9]{2,20}$")
_MEDIA = {"csv": "text/csv; charset=utf-8", "json": "application/json"}


def _symbol(text: str) -> str:
    s = text.strip().upper()
    if not _SYMBOL.match(s):
        raise ValueError(f"Parite yalnızca harf ve rakamdan oluşur, örneğin BTCUSDT. Yazılan: {text}")
    return s


def create_collector_app(store: CollectorStore, market_db: Path, market: MarketData, clock: Clock) -> FastAPI:
    """Toplayıcının uygulamasını kurar.

    Args:
        store: Toplayıcının yazılabilir deposu, parite ekleme ve çıkarma için.
        market_db: Okuma uçlarının salt okunur açtığı dosya.
        market: Eklenen paritenin Binance'te var olduğunu doğrulayan piyasa istemcisi.
        clock: Raporun anı ve paritenin eklenme zamanı için saat.
    """
    app = FastAPI()

    @app.exception_handler(ValueError)
    async def _bad(_: Request, e: ValueError):
        return JSONResponse({"detail": str(e)}, status_code=400)

    @app.get("/health")
    async def health():
        return {"ok": True, "time": clock.time(), "symbols": len(store.symbols())}

    @app.get("/summary")
    async def report():
        con = connect(market_db, readonly=True)
        try:
            return summary(con, clock.time())
        finally:
            con.close()

    @app.post("/symbols")
    async def add(symbol: str = Body(..., embed=True)):
        s = _symbol(symbol)
        if s in store.symbols():
            raise ValueError(f"{s} zaten izleniyor.")
        try:
            await market.premium(s)
        except MarketDataError as e:
            raise ValueError(f"{s} Binance'te doğrulanamadı, parite adı yanlış olabilir. Ayrıntı: {e}") from None
        store.add_symbol(s, clock.time())
        return {"symbols": store.symbols()}

    @app.post("/symbols/remove")
    async def remove(symbol: str = Body(..., embed=True)):
        s = _symbol(symbol)
        if s not in store.symbols():
            raise HTTPException(404, f"{s} izlenmiyor.")
        store.remove_symbol(s)
        return {"symbols": store.symbols()}

    # Satırlar olay döngüsünde parça parça okunur. Her parçadan sonra döngü
    # kaynaklara sıra verir, büyük bir indirme likidasyon akışını bekletmez.
    @app.get("/export")
    async def export(table: str, format: str = "csv", symbol: str | None = None, kind: str | None = None,
                     start: int | None = None, end: int | None = None):
        sym = _symbol(symbol) if symbol else None
        sql, args, cols = plan_export(table, format, sym, kind, start, end)
        con = connect(market_db, readonly=True)

        async def pieces():
            try:
                for piece in render(con.execute(sql, args), cols, format):
                    yield piece
                    await asyncio.sleep(0)
            finally:
                con.close()

        name = "-".join(x for x in (table, kind, sym) if x) + "." + format
        return StreamingResponse(pieces(), media_type=_MEDIA[format],
                                 headers={"Content-Disposition": f'attachment; filename="{name}"'})

    return app
