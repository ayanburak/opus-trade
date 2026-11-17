"""
file: account.py
importable: Account, AccountError

Account, tek bir dolar bakiyesini, katalogdaki paritelerde açılan pozisyonları,
bekleyen emirleri ve fiyat alarmlarını yönetir. Binance'in en küçük işlem
büyüklüğü, miktar adımı, maker ve taker komisyonu ve kayma uygulanır. Her
pozisyonun marjini izoledir. Pozisyona ekleme yapılabilir, pozisyon kısmen
kapatılabilir, birden çok TP kademesi ve iz süren stop tanımlanabilir. Bir
istek kurallara aykırıysa AccountError fırlatılır, mesajı doğrudan Opus'a
gösterilecek kadar açıktır. check_price() her fiyat güncellemesinde
likidasyon, SL, TP, iz süren stop, emir ve alarmları uygular. apply_funding()
bir paritenin fonlama ödemesini o paritedeki pozisyonlara yansıtır.
log_event() hesaptaki bir değişikliği yapanıyla olay tablosuna yazar.

description: Kâğıt üstünde kaldıraçlı vadeli işlemleri Binance kurallarıyla yürüten hesap.
"""

# NOTE: TP kademeleri pozisyonda mutlak miktar olarak saklanır. Opus yüzde
# verir, yüzde o anki pozisyon büyüklüğüne göre miktara çevrilir. Yüzdelerin
# toplamı yüz ise son kademe kalanın tamamı olarak saklanır, qty alanı None
# olur. Böylece yuvarlama yüzünden pozisyonun küçük bir kırıntısı açık kalmaz.
# Bekleyen emirlerde miktar henüz belli olmadığı için kademeler yüzde olarak
# saklanır ve emir gerçekleşince miktara çevrilir. Kademeler price, pct ve
# qty anahtarlı sözlüklerdir. Opus'un verdiği fiyat ve yuzde alanlı kademeler
# engine'de bu biçime çevrilir.
#
# NOTE: Kısmi kapatmada kapanan kısım ana pozisyondan ayrılıp kendi kapalı
# kaydına yazılır ve parent_id ile ana pozisyona bağlanır. Ana pozisyon
# küçülmüş haliyle açık kalır.
#
# NOTE: check_price() ve apply_funding() ürettikleri her olayı system
# kaynağıyla trade_events tablosuna yazar. İsteklerin kaydını ise kapı,
# engine/commands.py, kaynağıyla log_event() üzerinden yazar. İz süren stopun
# her kayışı ayrı olay olarak yazılmaz, trendde dakikada onlarca satır ederdi.
# Stop tetiklenirse seviyesi sl olayında zaten durur.
#
# NOTE: Her pozisyon gördüğü en iyi ve en kötü fiyatı zamanıyla tutar. Satır
# yalnızca bir şey değişince yazılır, fiyat aralığın içinde kaldıkça diske
# dokunulmaz. Kısmi kapanışta değerler kapanan parçaya da kopyalanır.
#
# NOTE: pnl alanı fiyat kârı ile fonlamanın toplamıdır, komisyon içermez.
# Komisyon fee alanında ayrı tutulur. Net sonuç pnl eksi fee'dir.
#
# NOTE: Kayma aleyhe işler. Long girişte fiyat yükselir, long çıkışta düşer.
# Maker sayılan işlemler, yani limit niteliğindeki emir girişleri ve TP
# kademeleri, kaymasız ve tam seviyesinden gerçekleşir. Likidasyonda kalan
# marjinin tamamı gittiği için kayma ve komisyon ayrıca hesaplanmaz.
#
# XXX: Hata ve olay mesajları Opus'a aynen gider. Mesajlarda geçen tp,
# tp_kademeleri ve yuzde gibi adlar Opus'un karar şemasındaki alan adlarıdır,
# kodla birlikte İngilizceye çevrilmemelidir.

import json
import math
import sqlite3

from .. import settings
from ..db import get_state, rows, set_state

# Kayan nokta hesaplarında kuruşun altındaki farklar yüzünden geçerli bir
# işlemin reddedilmemesi için kullanılan pay.
_TOLERANCE = 1e-9


# Hesap kurallarına aykırı her istek bu tiple reddedilir. Mesajı Türkçedir
# ve Opus'un bir sonraki çağrıda okuyacağı eylem sonucuna aynen yazılır.
class AccountError(Exception):
    pass


# Bütün durum veritabanında tutulur, nesnenin kendisi durum saklamaz. Böylece
# program yeniden başlatıldığında hesap kaldığı yerden devam eder. Profilden
# yalnızca başlangıç bakiyesi ve işlem yapılabilen pariteler kullanılır.
class Account:
    """Bakiye, pozisyonlar, bekleyen emirler ve alarmların tamamını yönetir.

    Args:
        con: db.connect() ile açılmış bağlantı.
        profile: Hesabın profili.
    """

    def __init__(self, con: sqlite3.Connection, profile: settings.Profile) -> None:
        self.con = con
        self.symbols = profile.symbols
        if get_state(con, "cash") is None:
            set_state(con, "cash", profile.balance)

    def _symbol(self, symbol: str) -> dict:
        s = self.symbols.get(symbol)
        if not s:
            raise AccountError(f"{symbol} katalogda yok. İşlem yapılabilen pariteler: {', '.join(self.symbols)}.")
        return s

    # Miktar adımın katına aşağı yuvarlanır. Binance de adımın katı olmayan
    # miktarı kabul etmez. Küçük bir pay, 0.03 gibi tam sayıların kayan nokta
    # yüzünden 0.029999 olup bir adım eksik yuvarlanmasını önler.
    def _round_step(self, symbol: str, qty: float) -> float:
        step = self.symbols[symbol]["qty_step"]
        return round(math.floor(qty / step + 1e-6) * step, 10)

    def _slipped(self, symbol: str, price: float, buy: bool) -> float:
        k = self.symbols[symbol]["slippage"]
        return price * (1 + k) if buy else price * (1 - k)

    # --- Okuma ---

    @property
    def cash(self) -> float:
        return get_state(self.con, "cash")

    def _set_cash(self, value: float) -> None:
        set_state(self.con, "cash", round(value, 8))

    def _read(self, table: str, status: str, symbol: str | None) -> list[dict]:
        if symbol:
            out = rows(self.con, f"SELECT * FROM {table} WHERE status = ? AND symbol = ? ORDER BY id", (status, symbol))
        else:
            out = rows(self.con, f"SELECT * FROM {table} WHERE status = ? ORDER BY id", (status,))
        for r in out:
            if "tp_levels" in r:
                r["tp_levels"] = json.loads(r["tp_levels"])
        return out

    def positions(self, symbol: str | None = None) -> list[dict]:
        return self._read("trades", "open", symbol)

    def pending_orders(self, symbol: str | None = None) -> list[dict]:
        return self._read("orders", "pending", symbol)

    def alerts(self, symbol: str | None = None) -> list[dict]:
        return self._read("alerts", "pending", symbol)

    def _position(self, id_: int) -> dict:
        found = [p for p in self.positions() if p["id"] == id_]
        if not found:
            raise AccountError(f"{id_} numaralı açık pozisyon yok.")
        return found[0]

    def _trade(self, id_: int) -> dict:
        r = rows(self.con, "SELECT * FROM trades WHERE id = ?", (id_,))[0]
        r["tp_levels"] = json.loads(r["tp_levels"])
        return r

    @staticmethod
    def unrealized_pnl(pos: dict, price: float) -> float:
        side = 1 if pos["side"] == "long" else -1
        return (price - pos["entry_price"]) * pos["qty"] * side

    # Özkaynak, bütün pozisyonlar şu an kapatılsa elde kalacak paraya yakın
    # bir değerdir. Kapanış komisyonu ve kayma bu hesaba katılmaz.
    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + sum(p["margin"] + self.unrealized_pnl(p, prices[p["symbol"]]) for p in self.positions())

    def stats(self) -> dict:
        closed = rows(self.con, "SELECT pnl, fee FROM trades WHERE status = 'closed'")
        nets = [c["pnl"] - c["fee"] for c in closed]
        return {
            "trade_count": len(closed),
            "wins": sum(1 for n in nets if n > 0),
            "losses": sum(1 for n in nets if n <= 0),
            "total_net": sum(nets),
            "total_fee": sum(r["fee"] for r in rows(self.con, "SELECT fee FROM trades")),
            "total_funding": sum(r["funding"] for r in rows(self.con, "SELECT funding FROM trades")),
        }

    # --- Hesaplama ---

    # Likidasyon, pozisyonun marjini ile açık zararının toplamının bakım
    # marjına düştüğü fiyattır. Marjin üzerinden hesaplandığı için ekleme,
    # kısmi kapatma ve fonlama sonrası da aynı formül doğru sonucu verir.
    def liq_price(self, symbol: str, side: str, entry: float, qty: float, margin: float) -> float:
        mm = self.symbols[symbol]["maint_margin"]
        if side == "long":
            return max(0.0, (entry * qty - margin) / (qty * (1 - mm)))
        return (entry * qty + margin) / (qty * (1 + mm))

    # Bir paritede açılabilecek en küçük büyüklük, Binance'in en küçük
    # büyüklüğü ile bir miktar adımının değerinden büyük olanıdır. BTC'de
    # 0.001 BTC çoğu zaman elli dolardan fazla ettiği için belirleyici olan adımdır.
    def min_size(self, symbol: str, price: float) -> float:
        s = self.symbols[symbol]
        step = s["qty_step"]
        return max(math.ceil(s["min_notional"] / price / step - 1e-9) * step, step) * price

    # --- Doğrulama ---

    def _check_leverage(self, symbol: str, leverage: float) -> None:
        limit = self.symbols[symbol]["max_leverage"]
        if not 1 <= leverage <= limit:
            raise AccountError(f"{symbol} için kaldıraç 1 ile {limit} arasında olmalı, {leverage} verildi.")

    @staticmethod
    def _check_sl(side: str, ref: float, sl: float | None) -> None:
        if sl is None:
            return
        if side == "long" and sl >= ref:
            raise AccountError(f"Long için SL {sl} referans fiyat {ref:g} altında olmalı.")
        if side == "short" and sl <= ref:
            raise AccountError(f"Short için SL {sl} referans fiyat {ref:g} üstünde olmalı.")

    @staticmethod
    def _check_trail(trail: float | None) -> None:
        if trail is not None and not 0 < trail <= 50:
            raise AccountError("İz süren stop mesafesi yüzde olarak sıfırdan büyük ve en fazla 50 olmalı.")

    # İz süren stopun seviyesi pozisyonun açılıştan beri gördüğü en iyi
    # fiyattan hesaplanır, long'da bu en yüksek, short'ta en düşük fiyattır.
    @staticmethod
    def _trail_level(side: str, best: float, trail: float) -> float:
        return best * (1 - trail / 100) if side == "long" else best * (1 + trail / 100)

    # tp tek bir sayıysa tamamını kapatan tek kademe demektir. levels
    # verildiyse her kademe bir fiyat ve pozisyonun yüzdesi taşır. Fiyatlar
    # referansın doğru tarafında olmalı ve yüzdelerin toplamı yüzü geçmemeli.
    @staticmethod
    def _levels(side: str, ref: float, tp: float | None, levels: list[dict] | None) -> list[dict]:
        if tp and levels:
            raise AccountError("tp ve tp_kademeleri birlikte verilemez, birini seç.")
        items = [{"price": tp, "pct": 100}] if tp else list(levels or [])
        total = 0.0
        for k in items:
            p, pct = k.get("price"), k.get("pct")
            if not p or not pct or pct <= 0:
                raise AccountError("Her TP kademesinde sıfırdan büyük fiyat ve yuzde olmalı.")
            if (side == "long" and p <= ref) or (side == "short" and p >= ref):
                where = "üstünde" if side == "long" else "altında"
                raise AccountError(f"{side} için TP {p} referans fiyat {ref:g} {where} olmalı.")
            total += pct
        if total > 100 + _TOLERANCE:
            raise AccountError(f"TP kademelerinin yüzdeleri toplamı en fazla 100 olabilir, {total:g} verildi.")
        return sorted(({"price": float(k["price"]), "pct": float(k["pct"])} for k in items),
                      key=lambda k: k["price"], reverse=(side == "short"))

    # Yüzdeli kademeleri pozisyonun miktarına göre mutlak miktara çevirir.
    # Toplam yüzse son kademe kalanın tamamıdır.
    def _levels_to_qty(self, symbol: str, levels: list[dict], qty: float) -> list[dict]:
        total = sum(k["pct"] for k in levels)
        out = []
        for i, k in enumerate(levels):
            last_full = i == len(levels) - 1 and total >= 100 - _TOLERANCE
            q = None if last_full else self._round_step(symbol, qty * k["pct"] / 100)
            if q == 0:
                raise AccountError(f"TP kademesi {k['price']} için yüzde {k['pct']:g} miktarı en küçük adımın "
                                   f"altında kalıyor. Kademeyi büyüt ya da kademe sayısını azalt.")
            out.append({"price": k["price"], "pct": k["pct"], "qty": q})
        return out

    # Marjin ve kaldıraçtan miktar çıkarılır, adıma yuvarlanır ve en küçük
    # büyüklük kontrol edilir. Gerçek marjin yuvarlanmış miktara göre yeniden
    # hesaplanır. Nakit yetmezse ama fark yüzde ikiden küçükse miktar nakde
    # sığacak kadar küçültülür. Opus genelde bakiyenin tamamını kullanmak için
    # yuvarlak bir sayı verir, komisyon yüzünden reddedilmesi anlamsız olurdu.
    def _size(self, symbol: str, leverage: float, margin: float, entry: float,
              fee_rate: float) -> tuple[float, float, float]:
        if margin <= 0:
            raise AccountError("Marjin sıfırdan büyük olmalı.")
        qty = self._round_step(symbol, margin * leverage / entry)
        needed = lambda q: q * entry / leverage + q * entry * fee_rate
        if needed(qty) > self.cash + _TOLERANCE:
            most = self._round_step(symbol, self.cash / (entry / leverage + entry * fee_rate))
            if needed(qty) > self.cash * 1.02 or most <= 0:
                raise AccountError(f"Yetersiz nakit. Nakit {self.cash:.2f} dolar, bu işlem için "
                                   f"{needed(qty):.2f} dolar gerekiyor.")
            qty = most
        smallest = self.min_size(symbol, entry)
        if qty * entry < smallest - _TOLERANCE:
            raise AccountError(
                f"{symbol} için en küçük işlem büyüklüğü yaklaşık {smallest:.2f} dolar. Bu marjin ve kaldıraçla "
                f"büyüklük {qty * entry:.2f} dolar oluyor. {leverage:g}x kaldıraçla en az "
                f"{smallest / leverage:.2f} dolar marjin gerekir."
            )
        return qty, qty * entry / leverage, qty * entry * fee_rate

    # --- Pozisyon işlemleri ---

    def open_position(
        self, symbol: str, side: str, leverage: int, margin: float, price: float, time: int,
        tp: float | None = None, tp_levels: list[dict] | None = None, sl: float | None = None,
        trail_pct: float | None = None, order_id: int | None = None, maker: bool = False,
    ) -> dict:
        """Yeni bir pozisyon açar.

        Args:
            symbol: Katalogdaki bir parite.
            side: long ya da short.
            leverage: Bir ile paritenin sınırı arasında tam sayı.
            margin: Pozisyona ayrılacak dolar. Gerçek marjin miktar yuvarlaması yüzünden biraz az olabilir.
            price: Piyasa fiyatı ya da emrin tetik fiyatı.
            time: Unix saniye.
            tp: Tamamını kapatan tek TP seviyesi.
            tp_levels: price ve pct alanlı kademeler. tp ile birlikte verilemez.
            sl: Zarar durdur seviyesi.
            trail_pct: İz süren stopun en iyi fiyattan yüzde uzaklığı.
            order_id: Pozisyon bekleyen bir emirden açılıyorsa o emrin numarası.
            maker: True ise kaymasız ve maker komisyonuyla açılır.

        Returns:
            Açılan pozisyonun kaydı.

        Raises:
            AccountError: Herhangi bir kurala aykırıysa.
        """
        self._symbol(symbol)
        self._check_leverage(symbol, leverage)
        entry = price if maker else self._slipped(symbol, price, side == "long")
        rate = settings.FEE_MAKER if maker else settings.FEE_TAKER
        qty, margin, fee = self._size(symbol, leverage, margin, entry, rate)
        self._check_sl(side, entry, sl)
        self._check_trail(trail_pct)
        levels = self._levels_to_qty(symbol, self._levels(side, entry, tp, tp_levels), qty)

        self._set_cash(self.cash - margin - fee)
        cur = self.con.execute(
            "INSERT INTO trades (symbol, side, leverage, margin, qty, entry_price, entry_time, tp_levels, "
            "sl, trail_pct, best_price, best_time, worst_price, worst_time, liq_price, fee, order_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (symbol, side, leverage, margin, qty, entry, time, json.dumps(levels), sl, trail_pct, entry, time, entry,
             time, self.liq_price(symbol, side, entry, qty, margin), fee, order_id),
        )
        self.con.commit()
        return self._trade(cur.lastrowid)

    # Ekleme bir parça yeni pozisyon gibi düşünülür, kendi marjini ve
    # kaldıracı vardır, piyasa fiyatından taker olarak gerçekleşir. Giriş
    # fiyatı miktara göre ağırlıklı ortalanır, pozisyonun kaldıracı da toplam
    # büyüklüğün toplam marjine oranı olur. TP kademelerine dokunulmaz, Opus
    # gerekirse onları ayrıca günceller.
    def add_to_position(self, id_: int, leverage: int, margin: float, price: float) -> dict:
        """Açık bir pozisyona piyasa fiyatından ekleme yapar.

        Args:
            id_: Pozisyon numarası.
            leverage: Eklenen parçanın kaldıracı.
            margin: Eklenen parçanın marjini.
            price: Güncel fiyat.

        Returns:
            Pozisyonun güncel kaydı.

        Raises:
            AccountError: Pozisyon yoksa ya da kurala aykırıysa.
        """
        p = self._position(id_)
        s = p["symbol"]
        self._check_leverage(s, leverage)
        entry = self._slipped(s, price, p["side"] == "long")
        add_qty, add_margin, fee = self._size(s, leverage, margin, entry, settings.FEE_TAKER)
        qty = round(p["qty"] + add_qty, 10)
        avg = (p["entry_price"] * p["qty"] + entry * add_qty) / qty
        total_margin = p["margin"] + add_margin

        self._set_cash(self.cash - add_margin - fee)
        self.con.execute(
            "UPDATE trades SET qty = ?, entry_price = ?, margin = ?, leverage = ?, liq_price = ?, "
            "fee = fee + ? WHERE id = ?",
            (qty, avg, total_margin, round(avg * qty / total_margin, 2),
             self.liq_price(s, p["side"], avg, qty, total_margin), fee, id_),
        )
        self.con.commit()
        return self._position(id_)

    def close_position(
        self, id_: int, price: float, time: int, reason: str, ratio: float = 1.0, qty: float | None = None,
    ) -> dict:
        """Açık bir pozisyonun tamamını ya da bir kısmını kapatır.

        Args:
            id_: Pozisyon numarası.
            price: Çıkış seviyesi. Neden agent, sl ya da period_end ise üzerine kayma uygulanır.
            time: Unix saniye.
            reason: agent, tp, sl, liquidation ya da period_end.
            ratio: Kapatılacak kısım, sıfır ile bir arası. qty verilirse yok sayılır.
            qty: Kapatılacak mutlak miktar. Pozisyondan büyükse tamamı kapanır.

        Returns:
            Kapanan kısmın kaydı, pnl ve fee dahil.

        Raises:
            AccountError: Pozisyon yoksa ya da kapatılacak miktar en küçük adımın altındaysa.
        """
        p = self._position(id_)
        s = p["symbol"]
        if qty is None:
            if not 0 < ratio <= 1:
                raise AccountError("Kapatma yüzdesi sıfırdan büyük ve en fazla 100 olmalı.")
            qty = p["qty"] if ratio >= 0.999 else self._round_step(s, p["qty"] * ratio)
        qty = min(qty, p["qty"])
        if qty <= 0:
            raise AccountError(f"Kapatılacak miktar {s} için en küçük adımın altında kalıyor.")
        # Kapanıştan sonra bir adımdan az kalacaksa pozisyonun tamamı kapatılır.
        if p["qty"] - qty < self.symbols[s]["qty_step"] - 1e-12:
            qty = p["qty"]
        full = qty >= p["qty"]
        share = qty / p["qty"]

        margin, funding, open_fee = p["margin"] * share, p["funding"] * share, p["fee"] * share
        if reason == "liquidation":
            exit_, pnl, close_fee = price, -margin - funding, 0.0
        else:
            maker = reason == "tp"
            exit_ = price if maker else self._slipped(s, price, p["side"] == "short")
            side = 1 if p["side"] == "long" else -1
            price_pnl = (exit_ - p["entry_price"]) * qty * side
            close_fee = qty * exit_ * (settings.FEE_MAKER if maker else settings.FEE_TAKER)
            self._set_cash(self.cash + margin + price_pnl - close_fee)
            pnl = price_pnl - funding

        if full:
            self.con.execute(
                "UPDATE trades SET exit_price = ?, exit_time = ?, exit_reason = ?, pnl = ?, "
                "fee = fee + ?, status = 'closed' WHERE id = ?",
                (exit_, time, reason, pnl, close_fee, id_),
            )
            closed = id_
        else:
            rest = 1 - share
            self.con.execute(
                "UPDATE trades SET qty = ?, margin = margin * ?, funding = funding * ?, fee = fee * ? WHERE id = ?",
                (round(p["qty"] - qty, 10), rest, rest, rest, id_),
            )
            cur = self.con.execute(
                "INSERT INTO trades (symbol, side, leverage, margin, qty, entry_price, entry_time, tp_levels, sl, "
                "trail_pct, best_price, best_time, worst_price, worst_time, liq_price, exit_price, exit_time, "
                "exit_reason, pnl, fee, funding, order_id, parent_id, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'closed')",
                (s, p["side"], p["leverage"], margin, qty, p["entry_price"], p["entry_time"],
                 json.dumps(p["tp_levels"]), p["sl"], p["trail_pct"], p["best_price"], p["best_time"],
                 p["worst_price"], p["worst_time"], p["liq_price"], exit_, time, reason, pnl,
                 open_fee + close_fee, funding, p["order_id"], id_),
            )
            closed = cur.lastrowid
        self.con.commit()
        return self._trade(closed)

    def update_protection(
        self, id_: int, price: float, tp: float | None = None, tp_levels: list[dict] | None = None,
        sl: float | None = None, trail_pct: float | None = None,
    ) -> dict:
        """Açık bir pozisyonun TP, SL ya da iz süren stopunu değiştirir.

        Verilmeyen alan değişmez. tp sıfır verilirse bütün TP kademeleri, sl
        sıfır verilirse SL, trail_pct sıfır verilirse iz süren stop kaldırılır.

        Args:
            id_: Pozisyon numarası.
            price: Güncel fiyat. Yeni seviyeler buna göre doğrulanır.
            tp: Tamamını kapatan tek TP seviyesi.
            tp_levels: price ve pct alanlı kademeler, yüzdeler kalan pozisyona göredir.
            sl: Yeni SL.
            trail_pct: Yeni iz süren stop mesafesi.

        Returns:
            Pozisyonun güncel kaydı.

        Raises:
            AccountError: Pozisyon yoksa ya da seviye geçersizse.
        """
        p = self._position(id_)
        levels, new_sl, new_trail = p["tp_levels"], p["sl"], p["trail_pct"]
        if tp == 0 and not tp_levels:
            levels = []
        elif tp or tp_levels:
            levels = self._levels_to_qty(p["symbol"], self._levels(p["side"], price, tp, tp_levels), p["qty"])
        if sl is not None:
            new_sl = sl or None
            self._check_sl(p["side"], price, new_sl)
        if trail_pct is not None:
            new_trail = trail_pct or None
            self._check_trail(new_trail)
        if trail_pct and new_trail:
            new_sl = self._trailed_sl(p["side"], p["best_price"], new_trail, new_sl, price)
        self.con.execute("UPDATE trades SET tp_levels = ?, sl = ?, trail_pct = ? WHERE id = ?",
                         (json.dumps(levels), new_sl, new_trail, id_))
        self.con.commit()
        return self._position(id_)

    # İz süren stop sonradan eklenince en iyi fiyat sıfırlanmaz, pozisyonun
    # açılıştan beri gördüğü en iyi fiyat kullanılır. Fiyat o en iyi noktadan
    # verilen yüzdeden fazla geri döndüyse stop daha kurulurken fiyatın
    # gerisinde kalır ve pozisyon hemen kapanırdı. Bu istek reddedilir, mesajda
    # verilebilecek en küçük yüzde yazar. İz mevcut SL'den daha yakındaysa
    # SL hemen oraya taşınır, ilk fiyat güncellemesi beklenmez.
    def _trailed_sl(self, side: str, best: float, trail: float, sl: float | None, price: float) -> float | None:
        level = self._trail_level(side, best, trail)
        if (level >= price) if side == "long" else (level <= price):
            least = (1 - price / best) * 100 if side == "long" else (price / best - 1) * 100
            raise AccountError(
                f"İz süren stop yüzde {trail:g} verildi. Pozisyonun açılıştan beri gördüğü en iyi fiyat {best:g}, "
                f"buna göre stop {level:g} seviyesine düşüyor ve güncel fiyat {price:g} bunu zaten geçmiş. "
                f"Bu pozisyonda iz süren stop en az yüzde {least:.2f}'den büyük olmalı.")
        if not sl or (level > sl if side == "long" else level < sl):
            return level
        return sl

    # --- Emirler ve alarmlar ---

    # Emrin koşulu verildiği andaki fiyata göre belirlenir. Tetik güncel
    # fiyatın altındaysa fiyat düşüp oraya değince, üstündeyse yükselip
    # oraya değince gerçekleşir. Fiyatın lehine geldiği emir, yani long için
    # aşağıdaki, short için yukarıdaki tetik, limit emri gibi maker sayılır.
    # Ters yöndeki kırılım emri stop emri gibi taker sayılır ve kayma yer.
    def place_order(
        self, symbol: str, side: str, trigger: float, leverage: int, margin: float, price: float, time: int,
        tp: float | None = None, tp_levels: list[dict] | None = None, sl: float | None = None,
        trail_pct: float | None = None, valid_min: int | None = None,
    ) -> dict:
        """Fiyat tetik seviyesine gelince yeni pozisyon açacak bekleyen bir emir kaydeder.

        Args:
            symbol: Katalogdaki bir parite.
            side: long ya da short.
            trigger: Emrin gerçekleşeceği fiyat.
            leverage: Bir ile paritenin sınırı arasında tam sayı.
            margin: Pozisyona ayrılacak dolar. Nakit, emir gerçekleşince kontrol edilir.
            price: Güncel fiyat.
            time: Unix saniye.
            tp: Tamamını kapatan tek TP seviyesi.
            tp_levels: price ve pct alanlı kademeler.
            sl: Zarar durdur seviyesi.
            trail_pct: İz süren stop mesafesi.
            valid_min: Bu kadar dakika içinde gerçekleşmezse emir iptal olur.

        Returns:
            Emrin kaydı.

        Raises:
            AccountError: Herhangi bir değer geçersizse.
        """
        self._symbol(symbol)
        self._check_leverage(symbol, leverage)
        if margin <= 0:
            raise AccountError("Marjin sıfırdan büyük olmalı.")
        if trigger <= 0 or abs(trigger - price) / price < 1e-5:
            raise AccountError("Tetik fiyatı güncel fiyattan farklı ve pozitif olmalı.")
        smallest = self.min_size(symbol, trigger)
        if self._round_step(symbol, margin * leverage / trigger) * trigger < smallest - _TOLERANCE:
            raise AccountError(f"{symbol} için en küçük işlem büyüklüğü tetik fiyatında yaklaşık {smallest:.2f} "
                               f"dolar. {leverage}x kaldıraçla en az {smallest / leverage:.2f} dolar marjin gerekir.")
        self._check_sl(side, trigger, sl)
        self._check_trail(trail_pct)
        levels = self._levels(side, trigger, tp, tp_levels)
        # Kademelerin miktara bölünüp bölünemediği normalde pozisyon açılırken
        # anlaşılır. Aynı hesap burada tetik fiyatındaki miktarla yapılır ki
        # hiç açılamayacak bir emir tetik anında değil kurulurken reddedilsin.
        # Tetik anında nakit azalmışsa miktar küçülüp yine takılabilir, o durum
        # check_price'taki order_failed yolundan Opus'a döner.
        self._levels_to_qty(symbol, levels, self._round_step(symbol, margin * leverage / trigger))
        if valid_min is not None and valid_min <= 0:
            raise AccountError("Geçerlilik süresi sıfırdan büyük olmalı.")
        condition = "below" if trigger < price else "above"
        maker = (side == "long") == (condition == "below")
        cur = self.con.execute(
            "INSERT INTO orders (symbol, side, trigger_price, condition, maker, leverage, margin, tp_levels, sl, "
            "trail_pct, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (symbol, side, trigger, condition, int(maker), leverage, margin, json.dumps(levels), sl, trail_pct,
             time + valid_min * 60 if valid_min else None, time),
        )
        self.con.commit()
        return [o for o in self.pending_orders() if o["id"] == cur.lastrowid][0]

    def cancel_order(self, order_id: int, time: int, note: str = "Opus iptal etti", status: str = "cancelled") -> None:
        """Bekleyen bir emri iptal eder.

        Raises:
            AccountError: Bu numarada bekleyen bir emir yoksa.
        """
        cur = self.con.execute(
            "UPDATE orders SET status = ?, resolved_at = ?, result_note = ? WHERE id = ? AND status = 'pending'",
            (status, time, note, order_id),
        )
        self.con.commit()
        if cur.rowcount == 0:
            raise AccountError(f"{order_id} numaralı bekleyen emir yok.")

    def set_alert(self, symbol: str, level: float, note: str, price: float, time: int) -> dict:
        """Fiyat seviyeye değince Opus'u çağıracak bir alarm kurar.

        Raises:
            AccountError: Parite katalogda yoksa ya da seviye geçersizse.
        """
        self._symbol(symbol)
        if level <= 0 or abs(level - price) / price < 1e-5:
            raise AccountError("Alarm seviyesi güncel fiyattan farklı ve pozitif olmalı.")
        cur = self.con.execute(
            "INSERT INTO alerts (symbol, price, condition, note, created_at) VALUES (?, ?, ?, ?, ?)",
            (symbol, level, "below" if level < price else "above", note, time),
        )
        self.con.commit()
        return [a for a in self.alerts() if a["id"] == cur.lastrowid][0]

    def cancel_alert(self, alert_id: int, time: int) -> None:
        cur = self.con.execute(
            "UPDATE alerts SET status = 'cancelled', resolved_at = ? WHERE id = ? AND status = 'pending'",
            (time, alert_id),
        )
        self.con.commit()
        if cur.rowcount == 0:
            raise AccountError(f"{alert_id} numaralı bekleyen alarm yok.")

    # --- Fiyat kontrolü ---

    # Kontrol tek bir fiyatla değil, son kontrolden bu yana görülen en
    # yüksek ve en düşük fiyatla yapılır. Böylece iki güncelleme arasındaki
    # ani iğneler de yakalanır. Aralık içinde fiyatın önce hangi uca gittiği
    # bilinemediği için temkinli sıra izlenir: önce zarar tarafı, en son TP
    # kademeleri. Zarar tarafında SL ve likidasyon aynı yöndedir, fiyat
    # hangisine önce varıyorsa pozisyon ondan kapanır. Long'da yüksek olan,
    # short'ta düşük olan seviye önce gelir. SL likidasyonun ötesine
    # konduysa likidasyon önce gelir. İz süren stop en son güncellenir,
    # böylece aynı aralıktaki yükseliş kendi stopunu aynı anda tetikleyemez.
    def check_price(self, symbol: str, high: float, low: float, time: int) -> list[dict]:
        """Bir paritenin son aralıktaki fiyatlarına göre pozisyon, emir ve alarmları işler.

        Args:
            symbol: Fiyatı gelen parite.
            high: Son kontrolden bu yana görülen en yüksek fiyat.
            low: Son kontrolden bu yana görülen en düşük fiyat.
            time: Unix saniye.

        Returns:
            Olan her şey için bir olay. Her olayda kind ve Türkçe bir message bulunur.
        """
        events: list[dict] = []
        name = self.symbols[symbol]["name"]
        for p in self.positions(symbol):
            long = p["side"] == "long"
            worst, best = (low, high) if long else (high, low)
            crossed = (lambda s: worst <= s) if long else (lambda s: worst >= s)
            reached = (lambda s: best >= s) if long else (lambda s: best <= s)

            # SL listede önce durur, iki seviye eşitse SL seçilir.
            stops = ([("sl", p["sl"])] if p["sl"] else []) + [("liquidation", p["liq_price"])]
            kind, level = (max if long else min)(stops, key=lambda x: x[1])
            if crossed(level):
                t = self.close_position(p["id"], level, time, kind)
                what = "likide oldu" if kind == "liquidation" else "SL ile kapandı"
                events.append({"kind": kind, "symbol": symbol, "trade": t,
                               "message": f"#{p['id']} {name} {p['side']} pozisyonu {t['exit_price']:g} seviyesinde "
                                          f"{what}, net {t['pnl'] - t['fee']:+.2f} dolar."})
                continue

            levels, done = list(p["tp_levels"]), False
            while levels and reached(levels[0]["price"]):
                lv = levels.pop(0)
                t = self.close_position(p["id"], lv["price"], time, "tp", qty=lv["qty"])
                done = t["id"] == p["id"]
                part = "tamamı" if done else f"{t['qty']:g} birimlik kısmı"
                events.append({"kind": "tp", "symbol": symbol, "trade": t,
                               "message": f"#{p['id']} {name} {p['side']} pozisyonunun {part} TP {lv['price']:g} "
                                          f"seviyesinde kapandı, net {t['pnl'] - t['fee']:+.2f} dolar."})
                if done:
                    break
            if done:
                continue
            # En iyi ve en kötü fiyat iz süren stop olsun olmasın her
            # güncellemede izlenir. Opus izi sonradan eklerse bu kayıttan
            # hesaplanır. Satır yalnızca bir şey değiştiyse yazılır, fiyat
            # yerinde saydıkça diske dokunulmaz.
            changes: dict = {}
            if levels != p["tp_levels"]:
                changes["tp_levels"] = json.dumps(levels)
            top = max(p["best_price"], best) if long else min(p["best_price"], best)
            if top != p["best_price"]:
                changes.update(best_price=top, best_time=time)
            bottom = min(p["worst_price"], worst) if long else max(p["worst_price"], worst)
            if bottom != p["worst_price"]:
                changes.update(worst_price=bottom, worst_time=time)
            if p["trail_pct"]:
                trail_sl = self._trail_level(p["side"], top, p["trail_pct"])
                if not p["sl"] or (trail_sl > p["sl"] if long else trail_sl < p["sl"]):
                    changes["sl"] = trail_sl
            if changes:
                self.con.execute(f"UPDATE trades SET {', '.join(f'{c} = ?' for c in changes)} WHERE id = ?",
                                 (*changes.values(), p["id"]))
                self.con.commit()

        for o in self.pending_orders(symbol):
            if o["expires_at"] and time >= o["expires_at"]:
                self.cancel_order(o["id"], time, "Geçerlilik süresi doldu", status="expired")
                events.append({"kind": "order_expired", "symbol": symbol, "order_id": o["id"], "message":
                               f"#{o['id']} {name} {o['side']} emrinin geçerlilik süresi doldu, emir iptal oldu."})
                continue
            hit = low <= o["trigger_price"] if o["condition"] == "below" else high >= o["trigger_price"]
            if not hit:
                continue
            try:
                trade = self.open_position(
                    symbol, o["side"], o["leverage"], o["margin"], o["trigger_price"], time,
                    tp_levels=o["tp_levels"] or None, sl=o["sl"], trail_pct=o["trail_pct"],
                    order_id=o["id"], maker=bool(o["maker"]),
                )
            except AccountError as e:
                self.cancel_order(o["id"], time, f"Tetiklendi ama açılamadı: {e}")
                events.append({"kind": "order_failed", "symbol": symbol, "order_id": o["id"],
                               "message": f"#{o['id']} {name} emri tetiklendi ama açılamadı: {e}"})
                continue
            self.con.execute(
                "UPDATE orders SET status = 'filled', resolved_at = ?, result_note = ? WHERE id = ?",
                (time, f"#{trade['id']} pozisyonu açıldı", o["id"]),
            )
            self.con.commit()
            events.append({"kind": "order_filled", "symbol": symbol, "trade": trade, "order_id": o["id"],
                           "message": f"#{o['id']} {name} {o['side']} emri {trade['entry_price']:g} fiyatından "
                                      f"gerçekleşti, #{trade['id']} pozisyonu açıldı."})

        for a in self.alerts(symbol):
            if (low <= a["price"]) if a["condition"] == "below" else (high >= a["price"]):
                self.con.execute("UPDATE alerts SET status = 'triggered', resolved_at = ? WHERE id = ?",
                                 (time, a["id"]))
                self.con.commit()
                events.append({"kind": "alert", "symbol": symbol, "alert_id": a["id"], "price": a["price"],
                               "message": f"Kurduğun #{a['id']} {name} {a['price']:g} alarmı tetiklendi. "
                                          f"Alarm notun: {a['note'] or 'yok'}"})
        self._log_all(events, time)
        return events

    # --- Fonlama ---

    # Oran pozitifse long'lar öder, short'lar alır. Negatifse tersi. Ödeme
    # pozisyonun o anki büyüklüğü üzerinden hesaplanır ve marjinden düşer.
    # Yalnızca fonlama anından önce açılmış pozisyonlar etkilenir.
    def apply_funding(self, symbol: str, rate: float, price: float, time: int) -> list[dict]:
        """Bir paritenin fonlama ödemesini o paritedeki açık pozisyonlara uygular.

        Args:
            symbol: Fonlaması gelen parite.
            rate: Binance'in açıkladığı fonlama oranı, örneğin 0.0001.
            price: Fonlama anındaki işaret fiyatı.
            time: Fonlama anı, unix saniye.

        Returns:
            Etkilenen her pozisyon için bir olay.
        """
        events = []
        for p in self.positions(symbol):
            if p["entry_time"] > time or not rate:
                continue
            payment = p["qty"] * price * rate * (1 if p["side"] == "long" else -1)
            margin = p["margin"] - payment
            if margin <= 0:
                t = self.close_position(p["id"], price, time, "liquidation")
                events.append({"kind": "liquidation", "symbol": symbol, "trade": t,
                               "message": f"#{p['id']} pozisyonu fonlama ödemesiyle marjini tükendiği için likide oldu."})
                continue
            self.con.execute(
                "UPDATE trades SET margin = ?, funding = funding + ?, liq_price = ? WHERE id = ?",
                (margin, payment, self.liq_price(symbol, p["side"], p["entry_price"], p["qty"], margin), p["id"]),
            )
            self.con.commit()
            events.append({"kind": "funding", "symbol": symbol, "position_id": p["id"], "price": price,
                           "detail": {"rate": rate, "payment": payment},
                           "message": f"#{p['id']} {symbol} pozisyonu fonlama {'ödedi' if payment > 0 else 'aldı'}: "
                                      f"{abs(payment):.4f} dolar, oran yüzde {rate * 100:.4f}."})
        self._log_all(events, time)
        return events

    # --- Olay kaydı ---

    def log_event(self, time: int, actor: str, kind: str, message: str, symbol: str | None = None,
                  position_id: int | None = None, order_id: int | None = None, alert_id: int | None = None,
                  price: float | None = None, detail: dict | None = None) -> None:
        """Hesaptaki bir değişikliği yapanıyla trade_events tablosuna yazar.

        Args:
            time: Unix saniye.
            actor: Değişikliği yapan: opus, admin ya da system.
            kind: Değişikliğin türü, örneğin open, close, tp, sl, funding.
            message: Opus'un ve arayüzün gördüğü Türkçe cümle.
            detail: Değişikliğin ayrıntısı, örneğin kapanan parçanın kaydı.
        """
        self.con.execute(
            "INSERT INTO trade_events (time, actor, kind, symbol, position_id, order_id, alert_id, price, message, "
            "detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time, actor, kind, symbol, position_id, order_id, alert_id, price, message,
             json.dumps(detail, ensure_ascii=False) if detail is not None else None),
        )
        self.con.commit()

    # Motorun kendi kuralıyla olan her şey system olarak yazılır. Kısmi bir
    # TP'de kapanan parça yeni bir satırdır, olay asıl pozisyonun numarasıyla
    # kaydedilir.
    def _log_all(self, events: list[dict], time: int) -> None:
        for e in events:
            t = e.get("trade")
            pos = (t.get("parent_id") or t["id"]) if t else e.get("position_id")
            price = (t.get("exit_price") or t.get("entry_price")) if t else e.get("price")
            self.log_event(time, "system", e["kind"], e["message"], e["symbol"], pos, e.get("order_id"),
                           e.get("alert_id"), price, t or e.get("detail"))
