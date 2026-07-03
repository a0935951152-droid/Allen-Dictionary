"""拼音 → IPA 音素序列 + 音素距離（聲學檢索層2＝規則式 NPD，§4.1/§5.6）。

層1 離散 pinyin_key 完全比對撈不到「未登錄的音近詞」（紅雞 key≠宏碁 key）；
層2 把詞拆成 **IPA 音素序列**，用**音素級加權編輯距離**算「發音有多近」——
連續距離（送氣差只算一個音素的小代價，非整個音節不同），未登錄的音近也召回。

純規則、純 CPU、零訓練（符 MISA 天條 1）。**G2P 派發器**（2026-07-01）：按書寫系統挑引擎——
- **漢字** → pypinyin(含聲調拼音 TONE3) → 本表(聲母/韻母→IPA)＋尾附聲調 token（真同音須含調全同）。
- **其他語言（拉丁/韓/泰/俄/阿/天城/坦米爾…）** → espeak-ng **字形直接→IPA**（規則式、離線、不經拼音）。
兩條都吐 IPA token，匯到同一個 `distance`/`_CONFUSE` 後端（本就語言無關）。共同語言是 IPA、不是拼音。
降級：缺 pypinyin → 漢字 no-op；缺 espeak-ng → 該語言逐字元 fallback（天條 3 不壞）。
"""
from __future__ import annotations

import functools
import re
import shutil
import subprocess

from ..config import settings
from . import langtag

try:
    from pypinyin import Style, lazy_pinyin
    _PY_OK = True
except Exception:  # pragma: no cover
    _PY_OK = False

try:                                    # 日文 G2P 前段：漢字+假名 → 平假名讀音（規則+字典，無訓練）
    import pykakasi
    _KKS = pykakasi.kakasi()
except Exception:  # pragma: no cover
    _KKS = None

# espeak-ng：非漢字語系的規則式 G2P（grapheme→IPA）。建構期 apt 裝、runtime 走本地資料離線。
_ESPEAK = shutil.which("espeak-ng") or shutil.which("espeak")
# ISO-15924 書寫系統 → espeak-ng 語音代碼（漢字不在此，走 pypinyin）。拉丁統一 en（近音比對只需一致性）。
_SCRIPT2VOICE = {
    "Latn": "en", "Hang": "ko", "Jpan": "ja", "Cyrl": "ru",
    "Thai": "th", "Arab": "ar", "Deva": "hi", "Taml": "ta",
}
_ESPEAK_TAG = re.compile(r"\([^)]*\)")                     # (en)…(cmn) 語言切換標記
_ESPEAK_STRIP = re.compile(r"[ˈˌːˑ'.\-\s0-9~‿|_]")        # 重音/長音/音節點/調號 → 去掉

# ── 國語拼音 → IPA（靜態表；聲母 + 韻母）──────────────────────────────
_INITIALS = [  # 長的先比（zh/ch/sh）
    ("zh", "ʈʂ"), ("ch", "ʈʂʰ"), ("sh", "ʂ"),
    ("b", "p"), ("p", "pʰ"), ("m", "m"), ("f", "f"),
    ("d", "t"), ("t", "tʰ"), ("n", "n"), ("l", "l"),
    ("g", "k"), ("k", "kʰ"), ("h", "x"),
    ("j", "tɕ"), ("q", "tɕʰ"), ("x", "ɕ"), ("r", "ʐ"),
    ("z", "ts"), ("c", "tsʰ"), ("s", "s"),
    ("y", "j"), ("w", "w"),
]
_FINALS = {
    "a": "a", "o": "o", "e": "ɤ", "ê": "ɛ", "ai": "ai", "ei": "ei", "ao": "au",
    "ou": "ou", "an": "an", "en": "ən", "ang": "aŋ", "eng": "əŋ", "ong": "ʊŋ", "er": "ɚ",
    "i": "i", "ia": "ja", "ie": "je", "iao": "jau", "iu": "jou", "iou": "jou",
    "ian": "jɛn", "in": "in", "iang": "jaŋ", "ing": "iŋ", "iong": "jʊŋ",
    "u": "u", "ua": "wa", "uo": "wo", "uai": "wai", "ui": "wei", "uei": "wei",
    "uan": "wan", "un": "wən", "uen": "wən", "uang": "waŋ", "ueng": "wəŋ",
    "ü": "y", "v": "y", "üe": "ɥe", "ve": "ɥe", "üan": "ɥɛn", "van": "ɥɛn",
    "ün": "yn", "vn": "yn",
}
# 整音節特例（空韻：zhi/chi/shi/ri 的 i＝舌尖後；zi/ci/si 的 i＝舌尖前）
_WHOLE = {"zhi": "ʈʂ ʐ̩", "chi": "ʈʂʰ ʐ̩", "shi": "ʂ ʐ̩", "ri": "ʐ ʐ̩",
          "zi": "ts z̩", "ci": "tsʰ z̩", "si": "s z̩",
          "yi": "i", "wu": "u", "yu": "y", "ye": "je", "yue": "ɥe", "yuan": "ɥɛn",
          "yin": "in", "yun": "yn", "ying": "iŋ"}

# 音素混淆類（同類互換＝低代價；對齊 ASR 近音：送氣/清濁、捲舌↔平舌、n/l、前後鼻音）
_CONFUSE = [
    {"p", "pʰ"}, {"t", "tʰ"}, {"k", "kʰ"}, {"tɕ", "tɕʰ"}, {"ts", "tsʰ"}, {"ʈʂ", "ʈʂʰ"},
    {"ts", "ʈʂ", "tɕ"}, {"tsʰ", "ʈʂʰ", "tɕʰ"}, {"s", "ʂ", "ɕ"}, {"ʐ", "l"}, {"n", "l"},
    {"n", "ŋ"}, {"ən", "əŋ"}, {"in", "iŋ"}, {"an", "aŋ"}, {"ʊ", "u"}, {"ɤ", "ə"}, {"o", "ou"},
]
_SUB_CONFUSE = settings.ipa_sub_confuse   # 同混淆類替換代價（vs 全不同＝1.0；env IPA_SUB_CONFUSE）
_SUB_TONE = settings.ipa_sub_tone         # 聲調 token 互換/增刪代價（含調全同＝0；env IPA_SUB_TONE）
_TONES = {"T1", "T2", "T3", "T4", "T5"}   # 聲調 token（明確集合，勿用前綴判斷——espeak fallback 可能吐字面 'T'）


def _split(syl: str) -> tuple[str, str]:
    for ini, _ in _INITIALS:
        if syl.startswith(ini):
            return ini, syl[len(ini):]
    return "", syl


def _han_tokens(text: str) -> list[str]:
    """漢字 → IPA 音素 token（拼音中介；**含聲調**）。缺 pypinyin → []（降級 no-op）。
    每音節尾附一個聲調 token（T1–T5，輕聲＝T5）：文字輸入聲調確定，讓「還是↔海蝕」這種
    只差聲調的假同音不再 d=0（見 _SUB_TONE）。`Style.TONE3` 拼音尾帶數字，須先剝再解析 base。"""
    if not _PY_OK or not text:
        return []
    out: list[str] = []
    for syl in lazy_pinyin(text, style=Style.TONE3, errors="ignore", neutral_tone_with_five=True):
        syl = syl.lower()
        tone = "T5"
        if syl and syl[-1].isdigit():                # 剝尾部聲調數字（base 音節才能查表）
            tone = "T" + syl[-1]
            syl = syl[:-1]
        if syl in _WHOLE:
            out += _WHOLE[syl].split()
            out.append(tone)
            continue
        ini, fin = _split(syl)
        if ini:
            out.append(dict(_INITIALS)[ini])
        ipa_fin = _FINALS.get(fin)
        if ipa_fin:
            out += list(ipa_fin)
        elif not ini:
            out += [c for c in syl]            # 無法解析（外語殘留）→ 逐字元，至少可比
        out.append(tone)
    return out


@functools.lru_cache(maxsize=8192)
def _espeak_tokens(run: str, voice: str) -> tuple[str, ...]:
    """非漢字子串 → espeak-ng grapheme→IPA → 清掉標記/調號 → IPA 字元 token（tuple 供快取）。
    缺 espeak-ng 或失敗 → 逐字元 fallback（至少可比，天條 3）。"""
    if not _ESPEAK or not run.strip():
        return tuple(run)
    try:
        res = subprocess.run([_ESPEAK, "-q", "--ipa", "-v", voice, run],
                             capture_output=True, text=True, timeout=5)
        ipa_str = res.stdout or ""
    except Exception:
        return tuple(run)
    ipa_str = _ESPEAK_STRIP.sub("", _ESPEAK_TAG.sub("", ipa_str))
    toks = tuple(c for c in ipa_str if c)
    return toks or tuple(run)


def _has_kana(text: str) -> bool:
    """含平/片假名 → 判為日文（日文漢字須讀日文音，不能當中文）。"""
    return any(langtag.char_script(c) == "Jpan" for c in text)


@functools.lru_cache(maxsize=4096)
def _ja_tokens(text: str) -> tuple[str, ...] | None:
    """日文 G2P **兩段**：pykakasi(漢字+假名→平假名讀音) → espeak ja(假名→IPA)。
    缺 pykakasi → None（讓 ipa_tokens 降級：假名段走 espeak、漢字段退中文讀）。"""
    if _KKS is None:
        return None
    try:
        hira = "".join(it["hira"] for it in _KKS.convert(text))
    except Exception:
        return None
    if not hira:
        return None
    return _espeak_tokens(hira, "ja")          # 平假名 espeak ja 品質佳（東京→とうきょう→to̞ɯkʲo̞ɯ）


def ipa_tokens(text: str) -> list[str]:
    """詞 → IPA 音素 token 序列（漢字含聲調 token；非漢字由 espeak 決定）。**G2P 派發**：
    - 含假名（日文）→ 整詞走日文兩段 G2P（pykakasi→espeak ja），漢字讀日文音。
    - 否則按書寫系統：漢字 pypinyin、其餘 espeak-ng；混語逐段轉、token 串接。全空 → []。"""
    if not text:
        return []
    if _has_kana(text):                        # 日文優先：整詞辨識（漢字須讀日文音，不可逐 script 拆給中文）
        ja = _ja_tokens(text)
        if ja is not None:
            return list(ja)
    out: list[str] = []
    for script, run in langtag.iter_script_runs(text):
        if script == "Han":
            out += _han_tokens(run)
        elif script in _SCRIPT2VOICE:
            out += list(_espeak_tokens(run, _SCRIPT2VOICE[script]))
        else:
            out += [c for c in run]            # 未知書寫系統 → 逐字元 fallback
    return out


def _is_han(s: str) -> bool:
    """整串皆漢字（逐字度量僅對漢字音節成立）。"""
    return bool(s) and all(langtag.char_script(c) == "Han" for c in s)


def to_ipa(text: str) -> str:
    """空白分隔的 IPA 字串（存索引用、可讀）。"""
    return " ".join(ipa_tokens(text))


def _sub_cost(a: str, b: str) -> float:
    if a == b:
        return 0.0
    if a in _TONES and b in _TONES:                       # 兩個都是聲調 token → 小代價
        return _SUB_TONE
    for cl in _CONFUSE:
        if a in cl and b in cl:
            return _SUB_CONFUSE
    return 1.0


def _indel_cost(t: str) -> float:
    """增刪代價：聲調 token 增刪＝小代價（跨書寫系統比對時，espeak 側無聲調 token，
    不能讓漢字側的聲調被當成整顆音素的插入懲罰）；音段 token＝1。"""
    return _SUB_TONE if t in _TONES else 1.0


def distance(a: list[str], b: list[str]) -> float:
    """兩 IPA 音素序列的**正規化加權編輯距離** ∈ [0,1]（0＝同音、越大越遠）。語言無關。
    **正規化分母只算音段 token（排除聲調）**：同調詞對的距離與無聲調時代完全一致（不因
    分母變大而變近＝召回面不擴大）；異調對只會變遠——聲調只能當懲罰，不能當稀釋。"""
    if not a and not b:
        return 0.0
    if not a or not b:
        return 1.0
    m, n = len(a), len(b)
    prev = [0.0] * (n + 1)
    for j in range(1, n + 1):
        prev[j] = prev[j - 1] + _indel_cost(b[j - 1])
    for i in range(1, m + 1):
        cur = [prev[0] + _indel_cost(a[i - 1])] + [0.0] * n
        for j in range(1, n + 1):
            cur[j] = min(prev[j] + _indel_cost(a[i - 1]),
                         cur[j - 1] + _indel_cost(b[j - 1]),
                         prev[j - 1] + _sub_cost(a[i - 1], b[j - 1]))
        prev = cur
    seg = max(sum(1 for t in a if t not in _TONES), sum(1 for t in b if t not in _TONES))
    return min(prev[n] / max(seg, 1), 1.0)


def word_distance(a: str, b: str) -> float:
    """**逐字** IPA 距離 ∈ [0,1]：字數相同→取各對應字 IPA 距離的**最大值**；字數不同→1.0。
    逐字（非整詞平均）避免共享前綴/後綴稀釋——豆腐沿↔豆腐腦：只『沿/腦』不同卻被『豆腐』拉低平均；
    取 max 讓任一字讀音差太遠即判遠。聲學檢索與同音閘門共用的單一逐字度量。
    **非漢字**（拼音文字等）無「逐字＝逐音節」對應 → 改用整詞 IPA 距離（G2P 直轉後比對）。"""
    if a == b:
        return 0.0
    if not (_is_han(a) and _is_han(b)):        # 非漢字：整詞 IPA 距離（逐字僅適用漢字）
        ta, tb = ipa_tokens(a), ipa_tokens(b)
        if not ta or not tb:
            return 1.0
        return distance(ta, tb)
    if len(a) != len(b):
        return 1.0
    mx = 0.0
    for ca, cb in zip(a, b):
        if ca == cb:
            continue
        ta, tb = ipa_tokens(ca), ipa_tokens(cb)
        if not ta or not tb:
            return 1.0
        d = distance(ta, tb)
        if d > mx:
            mx = d
    return mx
