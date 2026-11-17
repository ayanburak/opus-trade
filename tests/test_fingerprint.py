"""
file: test_fingerprint.py
importable: yok

Parmak izinin 17.11 kararlarına göre neye duyarlı olduğunu sınar: yorum,
docstring ve yeni eylem özeti değiştirmez, kodun kendisi, talimat ve istem
değiştirir. Proje klasöründe uv run pytest ile çalışır.

description: fingerprint modülünün sıfırlatan ve sıfırlatmayan değişiklikleri ayırdığını sınayan testler.
"""

import pytest

from opus_trade import fingerprint, settings
from opus_trade.opus import actions, agent

_CODE = '''"""Modül docstring'i."""

# Bir yorum.
def f(x):
    """Fonksiyon docstring'i."""
    return x + 1
'''


# Gerçek profiller public depoda bulunmadığı için testin profili elle kurulur.
_PROFILE = settings.Profile(id="test", name="Test Opus", short="Test", balance=100.0,
                            symbols={"BTCUSDT": settings.SYMBOLS["BTCUSDT"]}, period_s=86400, min_wait=5,
                            max_wait=240, style="")


@pytest.fixture
def source(tmp_path, monkeypatch):
    (tmp_path / "opus_trade").mkdir()
    monkeypatch.setattr(settings, "ROOT", tmp_path)
    path = tmp_path / "opus_trade" / "m.py"

    def digest(text: str) -> str:
        path.write_text(text)
        return fingerprint._source("m.py")
    return digest


def test_comments_and_docstrings_do_not_change_source(source):
    before = source(_CODE)
    changed = _CODE.replace("Bir yorum.", "Başka bir yorum.\n\n").replace("Fonksiyon docstring'i.", "Yeni metin.")
    assert source(changed) == before
    assert source(changed.replace('"""Modül docstring\'i."""\n', "")) == before


def test_code_change_changes_source(source):
    assert source(_CODE.replace("x + 1", "x + 2")) != source(_CODE)


# Eylem listesine yeni bir eylem eklenmiş gibi davranılır. Şema parçası
# değişmez, Opus'a giden şemanın kendisi değişir.
def test_new_action_does_not_change_schema_part(monkeypatch):
    p = _PROFILE
    before, full = fingerprint._digest(fingerprint._schema(p)), agent.schema(p)
    real = actions.schema
    monkeypatch.setattr(actions, "schema", lambda symbols: {**real(symbols), "yeni": True})
    assert agent.schema(p) != full
    assert fingerprint._digest(fingerprint._schema(p)) == before


def test_tools_are_not_part_of_fingerprint():
    keys = fingerprint.parts(_PROFILE).keys()
    assert "araclar" not in keys and not any("tools/" in k or "market/" in k for k in keys)
    assert {"profil", "talimat", "sema", "komut", "istem", "ayarlar"} <= keys
