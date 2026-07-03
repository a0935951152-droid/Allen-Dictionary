"""異體字前置（§3）：岩/巖 這類純字形變體先攤平，不進收斂迴圈（0 成本）。

以 OpenCC 做 best-effort 正規化；載入失敗則原樣回傳（封閉環境也能跑）。
"""
from __future__ import annotations

import re

# 標點/空白集合：切分用標點當「邊界」（便宜規則切），切完再把標點從存下的文字去掉。
_PUNCT = re.compile(
    r"[\s　，、。．‧·・！？!?；;：:,.…—─\-－~～「」『』（）()《》〈〉【】\[\]｛｝{}＂"
    r"\"'“”‘’`*#＃@＠/／\\｜|＿_…。]+"
)


def strip_punct(text: str) -> str:
    """去掉所有標點與空白，只留實字（中文/英數）。收斂用文字一律去標點以免對齊噪音。"""
    return _PUNCT.sub("", text or "")


try:
    from opencc import OpenCC

    # t2s→s2t 來回會把多數異體字摺疊到標準字形（best-effort）。
    _to_s = OpenCC("t2s")
    _to_t = OpenCC("s2t")
    _to_tw_cc = OpenCC("s2twp")     # 簡→繁 **＋** 大陸用語→台灣用語（軟件→軟體、信息→資訊、網路…）
    _ok = True
except Exception:  # pragma: no cover - 缺 opencc 時降級
    _ok = False


def flatten(text: str) -> str:
    if not _ok or not text:
        return text
    try:
        return _to_t.convert(_to_s.convert(text))
    except Exception:
        return text


def to_simp(text: str) -> str:
    """強制轉簡體（繁→簡，純字形）。查簡體詞表（如 jieba 詞頻）前的正規化。"""
    if not _ok or not text:
        return text
    try:
        return _to_s.convert(text)
    except Exception:
        return text


def to_trad(text: str) -> str:
    """強制轉繁體（簡→繁，純字形）。模型偶爾吐簡體字，這裡保證輸出繁中（比提示詞可靠）。"""
    if not _ok or not text:
        return text
    try:
        return _to_t.convert(text)
    except Exception:
        return text


def to_tw(text: str) -> str:
    """合規正規化（s2twp）：簡→繁 **＋** 大陸用語→台灣用語。攝取(中文錨)＋輸出(字典)統一字面。
    只動漢字（英/韓/拉丁原樣）；**但日文漢字會被誤轉 → 只可用在 cmn 文字，勿套用整段多語**。"""
    if not _ok or not text:
        return text
    try:
        return _to_tw_cc.convert(text)
    except Exception:
        return text


def same_after_flatten(a: str, b: str) -> bool:
    """兩個寫法攤平後是否相同（避免把純字形差異誤判成 model 變動）。"""
    return flatten(a) == flatten(b)
