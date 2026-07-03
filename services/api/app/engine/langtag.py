"""離線語言／書寫系統偵測（§11 多語擴充性標注）。

封閉環境：純 Unicode 區塊規則、無模型、無上網（對齊 GlotScript 的 ISO-15924 思路）。
代碼採 ISO-639-3（語言）+ ISO-15924（書寫系統），組成 BCP-47 風格 `cmn-Hant`。
語言代碼當 key 即可無限擴充（DaMuEL 模式）——加新語言＝多一個 Unicode 區塊規則，不動 schema。
"""
from __future__ import annotations

import re

# Unicode 區塊 → ISO-639-3 語言（書寫系統足以辨識者）。順序：先判獨佔區塊的語言。
_HIRA_KATA = re.compile(r"[぀-ヿ]")           # 平/片假名 → 日文獨有
_HANGUL = re.compile(r"[가-힣ᄀ-ᇿ]")  # 諺文 → 韓文
_HAN = re.compile(r"[一-鿿㐀-䶿]")     # 漢字（中/日共用，無假名則判中）
_LATIN = re.compile(r"[A-Za-zÀ-ɏ]")          # 拉丁（英/歐多語）
_CYRILLIC = re.compile(r"[Ѐ-ӿ]")
_THAI = re.compile(r"[฀-๿]")
_ARABIC = re.compile(r"[؀-ۿ]")
_DEVA = re.compile(r"[ऀ-ॿ]")        # 天城文 → 印地語
_TAMIL = re.compile(r"[஀-௿]")       # 坦米爾文 → 泰米爾語

# 繁簡粗判：常見簡化字出現 → Hans，否則中文預設 Hant（本專案多繁體）。
_SIMP_HINT = re.compile(r"[这国说对实现产发会无与时间们个为来]")


def _script_of_han(text: str) -> str:
    return "Hans" if _SIMP_HINT.search(text) else "Hant"


def langs_present(text: str) -> list[str]:
    """回傳文本中偵測到的 ISO-639-3 語言代碼清單（多語 chunk 會多個）。"""
    t = text or ""
    out: list[str] = []
    if _HIRA_KATA.search(t):
        out.append("jpn")
    if _HANGUL.search(t):
        out.append("kor")
    if _HAN.search(t) and "jpn" not in out:
        out.append("cmn")                 # 有漢字但無假名 → 視為中文
    elif _HAN.search(t) and "jpn" in out:
        pass                              # 漢字 + 假名 → 已記 jpn（日文含漢字）
    if _LATIN.search(t):
        out.append("eng")                 # 拉丁字母統記 eng（細分歐語需詞典，暫不做）
    if _CYRILLIC.search(t):
        out.append("rus")
    if _THAI.search(t):
        out.append("tha")
    if _ARABIC.search(t):
        out.append("ara")
    if _DEVA.search(t):
        out.append("hin")
    if _TAMIL.search(t):
        out.append("tam")
    return out or ["und"]                 # 未知 → und（ISO-639 undetermined）


def primary_lang(text: str) -> str:
    """主語言（取出現字元最多的語系）。多語對照時用來判 term 自身語言。"""
    t = text or ""
    counts = {
        "jpn": len(_HIRA_KATA.findall(t)),
        "kor": len(_HANGUL.findall(t)),
        "cmn": len(_HAN.findall(t)) if not _HIRA_KATA.search(t) else 0,
        "eng": len(_LATIN.findall(t)),
        "rus": len(_CYRILLIC.findall(t)),
        "tha": len(_THAI.findall(t)),
        "ara": len(_ARABIC.findall(t)),
        "hin": len(_DEVA.findall(t)),
        "tam": len(_TAMIL.findall(t)),
    }
    lang = max(counts, key=counts.get)
    return lang if counts[lang] > 0 else "und"


def lang_tag(text: str) -> str:
    """完整 BCP-47 風格標籤 `lang-Script`（如 cmn-Hant、eng-Latn、jpn-Jpan）。"""
    lang = primary_lang(text)
    script = {
        "cmn": _script_of_han(text), "jpn": "Jpan", "kor": "Hang",
        "eng": "Latn", "rus": "Cyrl", "tha": "Thai", "ara": "Arab",
        "hin": "Deva", "tam": "Taml",
    }.get(lang, LANGS.get(lang, {}).get("script", "Zyyy"))   # 退回註冊表；Zyyy = ISO-15924 undetermined
    return f"{lang}-{script}"


# ── 書寫系統分流（G2P 派發器用）：同一 chunk 混語時，逐段挑對應音韻引擎。───────────
# 順序：先判獨佔區塊（假名先於漢字，否則日文漢字會被當中文）。回 ISO-15924 script。
_SCRIPT_PATTERNS = [
    ("Jpan", _HIRA_KATA), ("Hang", _HANGUL), ("Han", _HAN),
    ("Latn", _LATIN), ("Cyrl", _CYRILLIC), ("Thai", _THAI),
    ("Arab", _ARABIC), ("Deva", _DEVA), ("Taml", _TAMIL),
]
_SPACE_KEEP = {"Latn", "Cyrl"}          # 有空白詞界的書寫系統：run 內保留空白（多詞片語一起送 G2P）


def char_script(ch: str) -> str:
    """單字元 → ISO-15924 書寫系統（Han/Latn/Hang/…）；標點/數字/空白 → Zyyy（無音）。"""
    for name, pat in _SCRIPT_PATTERNS:
        if pat.match(ch):
            return name
    return "Zyyy"


def iter_script_runs(text: str) -> list[tuple[str, str]]:
    """把文本切成 [(script, 連續同書寫系統子串), …]，供 G2P 逐段派發引擎。
    Zyyy（標點/數字）當分隔略去；拼音文字(Latn/Cyrl) run 內保留空白讓多詞一起轉。"""
    runs: list[tuple[str, str]] = []
    cur: str | None = None
    buf: list[str] = []
    for ch in text or "":
        sc = char_script(ch)
        if sc == "Zyyy":
            if cur in _SPACE_KEEP and ch.isspace():
                buf.append(ch)                      # 詞界空白：續接當前拼音 run
            elif buf:
                runs.append((cur, "".join(buf))); buf = []; cur = None
            continue
        if sc != cur:
            if buf:
                runs.append((cur, "".join(buf)))
            cur, buf = sc, [ch]
        else:
            buf.append(ch)
    if buf:
        runs.append((cur, "".join(buf)))
    return runs


# ── 語言註冊表（ISO-639-3 + ISO-15924）：規格(一) 多國互譯語系。──────────────────
# 「同步更新增加支援語系」＝在此加一列即可，不動 schema（DaMuEL 模式：語言代碼當 key 無限擴充）。
LANGS: dict[str, dict] = {
    "cmn": {"script": "Hant", "name": "國語",     "aliases": ("zh", "zho", "cmn", "mandarin", "chinese", "中文", "國語", "華語")},
    "eng": {"script": "Latn", "name": "英語",     "aliases": ("en", "eng", "english", "英文", "英語")},
    "jpn": {"script": "Jpan", "name": "日語",     "aliases": ("ja", "jpn", "jp", "japanese", "日文", "日語")},
    "kor": {"script": "Hang", "name": "韓語",     "aliases": ("ko", "kor", "korean", "韓文", "韓語")},
    "tha": {"script": "Thai", "name": "泰語",     "aliases": ("th", "tha", "thai", "泰文", "泰語")},
    "vie": {"script": "Latn", "name": "越南語",   "aliases": ("vi", "vie", "vietnamese", "越南文", "越語", "越南語")},
    "tgl": {"script": "Latn", "name": "他加祿語", "aliases": ("tl", "tgl", "fil", "tagalog", "filipino", "他加祿", "菲律賓語")},
    "ind": {"script": "Latn", "name": "印尼語",   "aliases": ("id", "ind", "indonesian", "印尼文", "印尼語")},
    "deu": {"script": "Latn", "name": "德語",     "aliases": ("de", "deu", "ger", "german", "德文", "德語")},
    "fra": {"script": "Latn", "name": "法語",     "aliases": ("fr", "fra", "fre", "french", "法文", "法語")},
    "spa": {"script": "Latn", "name": "西班牙語", "aliases": ("es", "spa", "spanish", "西班牙文", "西班牙語")},
    "por": {"script": "Latn", "name": "葡萄牙語", "aliases": ("pt", "por", "portuguese", "葡萄牙文", "葡萄牙語")},
    "ita": {"script": "Latn", "name": "義大利語", "aliases": ("it", "ita", "italian", "義大利文", "義大利語")},
    "nld": {"script": "Latn", "name": "荷蘭語",   "aliases": ("nl", "nld", "dut", "dutch", "荷蘭文", "荷蘭語")},
    "ces": {"script": "Latn", "name": "捷克語",   "aliases": ("cs", "ces", "cze", "czech", "捷克文", "捷克語")},
    "swe": {"script": "Latn", "name": "瑞典語",   "aliases": ("sv", "swe", "swedish", "瑞典文", "瑞典語")},
    "ltz": {"script": "Latn", "name": "盧森堡語", "aliases": ("lb", "ltz", "luxembourgish", "盧森堡語")},
    "slk": {"script": "Latn", "name": "斯洛伐克語", "aliases": ("sk", "slk", "slo", "slovak", "斯洛伐克語")},
    "pol": {"script": "Latn", "name": "波蘭語",   "aliases": ("pl", "pol", "polish", "波蘭文", "波蘭語")},
    "ukr": {"script": "Cyrl", "name": "烏克蘭語", "aliases": ("uk", "ukr", "ukrainian", "烏克蘭文", "烏克蘭語")},
    "rus": {"script": "Cyrl", "name": "俄語",     "aliases": ("ru", "rus", "russian", "俄文", "俄語")},
    "ara": {"script": "Arab", "name": "阿拉伯語", "aliases": ("ar", "ara", "arabic", "阿拉伯文", "阿拉伯語")},
    "tur": {"script": "Latn", "name": "土耳其語", "aliases": ("tr", "tur", "turkish", "土耳其文", "土耳其語")},
    "hin": {"script": "Deva", "name": "印地語",   "aliases": ("hi", "hin", "hindi", "印地文", "印地語")},
    "tam": {"script": "Taml", "name": "泰米爾語", "aliases": ("ta", "tam", "tamil", "泰米爾文", "泰米爾語")},
    "urd": {"script": "Arab", "name": "烏爾都語", "aliases": ("ur", "urd", "urdu", "烏爾都文", "烏爾都語")},
}

_LANG_ALIAS: dict[str, str] = {}
for _iso, _meta in LANGS.items():
    _LANG_ALIAS[_iso] = _iso
    for _a in _meta["aliases"]:
        _LANG_ALIAS[_a.lower()] = _iso

# 無空白詞界的書寫系統（限字數、不限詞數）；其餘拼音文字限字數+詞數。
_DENSE_SCRIPTS = {"Hant", "Hans", "Jpan", "Hang", "Thai"}


def canon_lang(key: str) -> str:
    """任意語言鍵（en／eng／english／英文）→ ISO-639-3（eng）。未知回空字串（不臆測）。"""
    return _LANG_ALIAS.get((key or "").strip().lower(), "")


def name_limits(iso: str) -> tuple[int, int]:
    """某語言『固定譯法』長度上限 (字元數, 詞數)；依書寫系統。詞數 0＝不限（CJK/泰文無空白）。"""
    script = LANGS.get(iso, {}).get("script", "Latn")
    return (16, 0) if script in _DENSE_SCRIPTS else (28, 4)
