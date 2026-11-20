"""
file: errors.py
importable: RuleError

RuleError, hesap kurallarına aykırı her isteğin hata tipidir. Mesajı Türkçedir
ve olduğu gibi gösterilecek kadar açıktır. Stratejiye, admin paneline ya da
Opus'a hiç değiştirilmeden iletilebilir.

description: Hesap kurallarının ortak hata tipi.
"""

# NOTE: Mesajlarda karar şemasının alan adları geçmez. Şema LLM stratejisinin
# işidir ve bir kural mesajı admin panelinde ya da kural stratejisinin
# günlüğünde de görünür. Opus'a özel bir ifade gerekiyorsa çeviriyi LLM
# stratejisi yapar.


# Kural ihlali bir program hatası değildir, isteği yapanın düzeltebileceği bir
# durumdur. Bu yüzden ValueError'dan değil doğrudan Exception'dan türer ve
# çağıran onu programın kendi hatalarından ayırarak yakalayabilir.
class RuleError(Exception):
    pass
