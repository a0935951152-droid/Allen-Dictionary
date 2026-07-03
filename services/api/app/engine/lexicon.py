"""通用詞判定（A1/A3/A5 共用；2026-07-02 結構審查）——jieba 內建詞頻表查表。

高頻通用詞（還是/等等/現在…）的先驗與 CAG 專名相反：surface 本身是常用詞時，
「這是誤聽」的假設需要**更強**證據，不能因曾登錄為變體／IPA 音近就無條件必收。
用途（三處守門，同一把尺）：
① `phonetic_index._build`：通用詞變體標 ambiguous，不進直掃必收層（仍可由 IPA 模糊層提名）。
② `refine.judge_span`：原詞為通用詞 → 判官算出「修」也封頂第④態人工（永不 auto）。
③ `main.commit/sync`：通用詞不進 `entry.variants`、sync regex 不吐（另列 contextual）。

詞頻來源＝jieba `dict.txt`（~35 萬詞含頻次；隨套件安裝、離線、規則查表、零訓練，符 MISA
天條 1/2）。jieba 詞表為簡體 → 查表前 `to_simp`。門檻走 `settings.common_fpm`（每百萬詞
次數，預設 10≈zipf 5；#3 參數外移規範）。降級：缺 jieba → 一律 False＝回退 A1 前行為（天條 3）。
"""
from __future__ import annotations

import functools

from ..config import settings
from .variants import strip_punct, to_simp

try:
    import jieba
    _J = jieba.dt
    _OK = True
except Exception:  # pragma: no cover
    _OK = False


@functools.lru_cache(maxsize=16384)
def freq_pm(word: str) -> float:
    """詞的**每百萬詞出現次數**（jieba 語料尺度）。查不到/降級 → 0.0。"""
    if not _OK or not word:
        return 0.0
    w = to_simp(strip_punct(word))
    if not w:
        return 0.0
    if not _J.initialized:
        _J.initialize()                        # lazy 載詞表（一次性，之後純 dict 查表）
    f = _J.FREQ.get(w, 0)                      # jieba 前綴項頻次=0 → 自然視為非詞
    return f * 1_000_000.0 / max(_J.total, 1)


def is_common_word(word: str) -> bool:
    """word 是否為高頻通用詞（≥ settings.common_fpm 每百萬詞）。
    還是/等等/現在 → True；海蝕/薑石/龍山是 → False（域內專名與不成詞字串不受影響）。"""
    return freq_pm(word) >= settings.common_fpm
