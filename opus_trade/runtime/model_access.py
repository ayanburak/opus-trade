"""
file: model_access.py
importable: ModelAccess

ModelAccess bütün LLM profillerinin ortak model erişimidir. close_until()
erişimi verilen ana kadar kapatır, open() hemen açar, closed() o anda kapalı
olup olmadığını söyler. report_error() bir çağrı hatasının metnine bakar,
limitin dolduğunu gösteriyorsa panelde görünecek uyarıyı yazar. Hata metni
yenilenme saatini söylüyorsa uyarı onu öneri olarak taşır.

description: Abonelik limiti dolunca LLM çağrılarının bekletilmesi.
"""

# NOTE: Saat ayarlarda saklanır, program yeniden başlasa da erişim kapalı kalır.
# Saat geçince erişim kendiliğinden açılır, ayrıca bir işlem gerekmez.
#
# NOTE: Uyarı kendiliğinden erişimi kapatmaz. Limitin ne zaman yenileneceğini
# Burak bilir ve girer, program yalnızca dikkatini çeker.
#
# XXX: Limit hatasının metni claude komutunun sürümüyle değişebilir. Tanınan
# ifadeler _LIMIT_HINTS'tedir, yeni bir ifade görülürse buraya eklenir.

import re
from datetime import datetime, time, timedelta

from ..domain.clock import HOME_TZ, Clock
from ..storage.runtime_state import StateRepo

_UNTIL = "model_closed_until"
_WARNING = "model_limit_warning"
_LIMIT_HINTS = ("usage limit", "rate limit", "limit reached", "hit your limit", "limit will reset", "5-hour limit")

# claude komutu limit hatasında yenilenme saatini "resets 6:40am" biçiminde
# yazıyor. Saat Türkiye saatiyle okunur ve geçmişteyse ertesi güne alınır.
_RESETS = re.compile(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)", re.IGNORECASE)


def _reset_time(text: str, now: int) -> int | None:
    m = _RESETS.search(text)
    if not m:
        return None
    hour, minute, half = int(m.group(1)) % 12, int(m.group(2) or 0), m.group(3).lower()
    hour += 12 if half == "pm" else 0
    today = datetime.fromtimestamp(now, HOME_TZ).date()
    at = datetime.combine(today, time(hour, minute), HOME_TZ)
    if at.timestamp() <= now:
        at += timedelta(days=1)
    return int(at.timestamp())


class ModelAccess:
    def __init__(self, state: StateRepo, clock: Clock) -> None:
        self.state = state
        self.clock = clock

    def closed(self) -> bool:
        until = self.state.setting(_UNTIL)
        return until is not None and self.clock.time() < until

    def until(self) -> int | None:
        until = self.state.setting(_UNTIL)
        return until if until is not None and self.clock.time() < until else None

    def close_until(self, when: int) -> None:
        self.state.set_setting(_UNTIL, int(when))
        self.state.set_setting(_WARNING, None)

    def open(self) -> None:
        self.state.set_setting(_UNTIL, None)

    def warning(self) -> dict | None:
        return self.state.setting(_WARNING)

    def report_error(self, text: str) -> bool:
        """Hata limitin dolduğunu gösteriyorsa uyarıyı yazar ve True döndürür."""
        if not any(h in text.lower() for h in _LIMIT_HINTS):
            return False
        now = self.clock.time()
        self.state.set_setting(_WARNING, {"time": now, "text": text[:500], "suggested_until": _reset_time(text, now)})
        return True
