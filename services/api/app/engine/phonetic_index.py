"""聲學檢索索引（§4.1 接地的音韻通道）——**單層 IPA**（2026-06-24：拆掉 pinyin_key 拼音層，都走 IPA）。

ASR 錯字「語意壞、讀音沒壞」→ 用**讀音**檢索正解。聲學表徵統一走 **IPA 音素序列 + 加權編輯距離**
（`ipa.py`；純規則、純 CPU、零訓練，符 MISA 天條），不再用離散拼音 key 比對（拼音文字層已移除）。

**雙門檻控過修**（聲學目標兩種來源、過修風險不同）：
- **CAG（is_special）**：IPA 距離 ≤ `_IPA_FUZZY`(0.28) **模糊召回**——不能錯的專名/術語，容未登錄音近（紅雞→宏碁）。
- **累積字典 + refterms 詞表**：IPA 距離 ≤ `_IPA_EXACT`(≈0) **真同音**——寬泛詞只收完全同音，避免被拿去模糊比對過度修正。

知識側來源（三處）：
1. 累積字典 `data/dictionary.json`（TermEntry.correct）— 真同音層。
2. 參考詞表 `data/files/refterms/*.{txt,json}` — 真同音層。
3. 知識側種子 `data/knowledge/acoustic.json`（§5.6）：CAG 正解 term + ASR 變體 — 模糊層。
   變體最關鍵——`紅雞→宏碁`（拼音不同、IPA 近）靠 IPA 距離撈回；已登錄變體另由 `scan_variants` 直接字串掃描。

**符全域正規化規範**：IPA 一律走 `ipa.ipa_tokens`、身分比對走 `variants.flatten`。種子只存原始字串、IPA 在此端算。
缺 pypinyin → IPA 索引/查詢皆 no-op（封閉環境降級，不壞）。
"""
from __future__ import annotations

import json
import os
import threading

from ..config import settings
from ..storage import store
from . import ipa, lexicon
from .variants import flatten as _flat

_REFTERMS_DIR = os.path.join(settings.data_dir, "files", "refterms")
_KNOW = os.path.join(settings.data_dir, "knowledge", "acoustic.json")    # 知識側聲學種子（build/relabel 寫）
_IPA_INDEX = os.path.join(settings.data_dir, "index", "acoustic", "ipa_index.json")  # IPA 索引落地（CPU 一次性）
_IPA_FUZZY = settings.ipa_fuzzy   # CAG 模糊召回門檻（容未登錄音近；env IPA_FUZZY）
_IPA_EXACT = settings.ipa_exact   # 字典/refterms 真同音門檻（IPA 完全相同＝距離 0；env IPA_EXACT）

_lock = threading.Lock()
_cache: dict | None = None          # {"sig", "ipa":[(term,tokens,is_cag)], "corrects":set, "variants":{v:c}, "know"}


# ── 參考語料載入（data/files/refterms）────────────────────────
def _load_refterms() -> list[str]:
    """讀 refterms 目錄下的正解詞表：.txt（每行一詞，# 註解略）、.json（["詞",…] 或 [{term/correct}]）。"""
    terms: list[str] = []
    if not os.path.isdir(_REFTERMS_DIR):
        return terms
    for fn in sorted(os.listdir(_REFTERMS_DIR)):
        path = os.path.join(_REFTERMS_DIR, fn)
        if not os.path.isfile(path):
            continue
        try:
            if fn.endswith(".txt"):
                with open(path, encoding="utf-8") as f:
                    terms += [ln.strip() for ln in f
                              if ln.strip() and not ln.lstrip().startswith("#")]
            elif fn.endswith(".json"):
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    data = data.get("terms", [])
                for it in data or []:
                    w = it if isinstance(it, str) else (it.get("term") or it.get("correct") or "")
                    if w:
                        terms.append(str(w).strip())
        except Exception:
            continue                # 壞檔不擋整個索引
    return terms


def _load_knowledge() -> list[tuple[str, list[str]]]:
    """讀知識側聲學種子 → [(正解, [變體,…]), …]，**只收 CAG（is_special）**＝模糊召回的聲學靶。"""
    try:
        with open(_KNOW, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    out: list[tuple[str, list[str]]] = []
    for r in data.get("terms", []):
        if not r.get("is_special"):
            continue
        correct = str(r.get("correct") or "").strip()
        if not correct:
            continue
        variants = [str(v).strip() for v in (r.get("variants") or []) if str(v).strip()]
        out.append((correct, variants))
    return out


def _signature() -> tuple:
    """便宜失效訊號：字典檔 + 知識側種子 mtime + refterms 目錄各檔 mtime。任一變動就重建。"""
    sig = []
    try:
        sig.append(("dict", os.path.getmtime(store.dict_path)))
    except OSError:
        pass
    try:
        sig.append(("know", os.path.getmtime(_KNOW)))
    except OSError:
        pass
    if os.path.isdir(_REFTERMS_DIR):
        for fn in sorted(os.listdir(_REFTERMS_DIR)):
            p = os.path.join(_REFTERMS_DIR, fn)
            if os.path.isfile(p):
                sig.append((fn, os.path.getmtime(p)))
    return tuple(sig)


def _build() -> dict:
    """建單層 IPA 索引：[(正解, IPA tokens, is_cag)]；CAG 走模糊門檻、字典/refterms 走真同音門檻。
    正解去重（重複時升級為 CAG＝較寬鬆）；變體只進 `variants` 直接掃描表。
    **變體分級（A1）**：變體本身是通用詞（還是/現在…）→ 進 `ambiguous`（曾登錄誤聽樣態，
    不直掃必收；仍可由 IPA 模糊層提名）；不成詞字串（龍山是）→ 進 `variants`（確定誤聽必收）。"""
    corrects: set[str] = set()               # 所有正解 flatten（is_target 判斷）
    variant_map: dict[str, str] = {}         # CAG 變體原字串 → 正解（scan_variants 用）
    ambiguous_map: dict[str, str] = {}       # 通用詞變體 → 正解（只當語境參考，永不必收）
    by_correct: dict[str, list] = {}         # flatten(correct) → [correct, tokens, is_cag]（去重）
    if not ipa._PY_OK:                       # 缺 pypinyin → 空索引（查詢 no-op）
        return {"sig": _signature(), "ipa": [], "corrects": corrects,
                "variants": variant_map, "ambiguous": ambiguous_map, "know": 0}

    def add_target(correct: str, is_cag: bool) -> None:
        cf = _flat(correct)
        corrects.add(cf)
        row = by_correct.get(cf)
        if row is not None:
            if is_cag:                       # 已存在 → 若這次是 CAG 則升級為模糊層
                row[2] = True
            return
        toks = ipa.ipa_tokens(correct)
        if toks:
            by_correct[cf] = [correct, toks, is_cag]

    # 1+2：字典 correct + refterms 詞表 → 真同音層（is_cag=False）
    terms: list[str] = []
    try:
        for t in store.load_dict().values():
            if t.correct:
                terms.append(t.correct)
    except Exception:
        pass
    terms += _load_refterms()
    for term in terms:
        add_target(term, False)

    # 3：知識側 CAG 種子 → 模糊層（is_cag=True）＋變體直接掃描表
    know = _load_knowledge()
    for correct, variants in know:
        add_target(correct, True)
        for v in variants:
            if _flat(v) != _flat(correct) and len(v) >= 2:
                # A1 變體分級：通用詞／本身是字典正解 ≠ 確定誤聽（corrects 此時已含字典+refterms）
                if lexicon.is_common_word(v) or _flat(v) in corrects:
                    ambiguous_map.setdefault(v, correct)
                else:
                    variant_map.setdefault(v, correct)

    ipa_list = list(by_correct.values())      # [[correct, tokens, is_cag], …]
    _write_ipa_index(ipa_list)
    return {"sig": _signature(), "ipa": ipa_list, "corrects": corrects,
            "variants": variant_map, "ambiguous": ambiguous_map, "know": len(know)}


def _write_ipa_index(ipa_list: list) -> None:
    """把 IPA 索引落地到 data/index/acoustic/ipa_index.json（糾正側，§1.2）。CPU 一次性。"""
    try:
        os.makedirs(os.path.dirname(_IPA_INDEX), exist_ok=True)
        payload = {"count": len(ipa_list), "fuzzy": _IPA_FUZZY, "exact": _IPA_EXACT,
                   "terms": [{"correct": c, "ipa": t, "cag": g} for c, t, g in ipa_list]}
        tmp = _IPA_INDEX + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, _IPA_INDEX)
    except Exception:
        pass


def _index() -> dict:
    """取索引（lazy + mtime 失效重建）。"""
    global _cache
    with _lock:
        sig = _signature()
        if _cache is None or _cache["sig"] != sig:
            _cache = _build()
        return _cache


# ── 對外 API ─────────────────────────────────────────────
def query(surface: str, limit: int = 8) -> list[str]:
    """單層 IPA 撈正解：對每個聲學靶算 IPA 音素距離，依其層別門檻（CAG 0.28 / 其餘 ≈0）收候選，
    依距離由近到遠回傳（排除與 surface 攤平後相同者）。聲學召回，交 ground/精修 融合驗證。"""
    cache = _index()
    sflat = _flat(surface)
    if not ipa.ipa_tokens(surface):
        return []
    scored: list[tuple[float, str]] = []
    seen: set[str] = set()
    for correct, _t_toks, is_cag in cache["ipa"]:
        if _flat(correct) == sflat or correct in seen:
            continue
        d = ipa.word_distance(surface, correct)        # 逐字 IPA（避免前綴稀釋）
        if d <= (_IPA_FUZZY if is_cag else _IPA_EXACT):
            scored.append((d, correct)); seen.add(correct)
    scored.sort(key=lambda x: x[0])
    return [c for _d, c in scored[:limit]]


def query_scored(surface: str, limit: int = 8) -> list[tuple[str, float, bool, float]]:
    """同 query，但回 (正解, IPA 距離, is_cag, 該層門檻)——供三報告聲學通道算正規化分數。"""
    cache = _index()
    sflat = _flat(surface)
    if not ipa.ipa_tokens(surface):
        return []
    scored: list[tuple[float, str, bool, float]] = []
    seen: set[str] = set()
    for correct, _t_toks, is_cag in cache["ipa"]:
        if _flat(correct) == sflat or correct in seen:
            continue
        d = ipa.word_distance(surface, correct)
        thr = _IPA_FUZZY if is_cag else _IPA_EXACT
        if d <= thr:
            scored.append((d, correct, is_cag, thr)); seen.add(correct)
    scored.sort(key=lambda x: x[0])
    return [(c, d, g, t) for d, c, g, t in scored[:limit]]


def is_target(surface: str) -> bool:
    """surface 本身是否已是索引內的正解（flatten 比對）。是＝這詞是對的、不該被當錯字改。"""
    return _flat(surface) in _index().get("corrects", set())


def variant_correct(surface: str) -> str | None:
    """surface 若為已登錄 CAG 變體（人工核可的確定誤聽）→ 回正解；否則 None。
    確定誤聽**不受 IPA 距離門檻限制**（如 龍山是→龍山寺 IPA 0.67 但已人工登錄，verified 必收，天條 4）。
    通用詞變體不在此（A1 分級 → `variant_ambiguous`）：登錄記錄的是「某句曾誤聽」＝條件事實，
    對常用詞不得升級成「任何語境都是誤聽」。"""
    return _index().get("variants", {}).get(surface)


def variant_ambiguous(surface: str) -> str | None:
    """surface 若為 ambiguous 變體（通用詞，曾登錄誤聽樣態）→ 回正解；否則 None。
    只當語境參考證據，不必收、不置頂、判定封頂人工。"""
    return _index().get("ambiguous", {}).get(surface)


def scan_variants(text: str) -> list[dict]:
    """在 text 直接掃描已登錄 CAG 變體字串（不靠 ner；確定誤聽）。回 [{surface,correct,start}]。
    **簡繁/異體正規化**：變體與文本都 flatten 後比對，深恒↔深恆、岩↔巖 不因字形漏掉。"""
    vm = _index().get("variants", {})
    out: list[dict] = []
    ntext = _flat(text)                                    # 正規化後的文本（比對用）
    for v, c in vm.items():
        nv = _flat(v)
        if not nv:
            continue
        start = ntext.find(nv)
        while start >= 0:
            surface = text[start:start + len(nv)] or v     # 回填原文字面（長度對齊）
            out.append({"surface": surface, "correct": c, "start": start})
            start = ntext.find(nv, start + 1)
    return out


def scan_terms(text: str, fuzzy_thr: float = _IPA_FUZZY) -> list[dict]:
    """**第三偵測器**（繞過 ner，語言無關優先）：對 CAG 正解詞表直掃 chunk。
    (a) 登錄變體 簡繁正規化直掃（= scan_variants，確定誤聽）。
    (b) **漢字 IPA 鄰域召回**：對每個 CAG 正解，滑同長漢字窗，IPA 距離 ≤ fuzzy_thr 即召回——
        補 ner 沒圈到的未登錄音近（生核化石→生痕化石），不靠 NER 認專名。
    回 [{surface, correct, start, via}]（via=variant|ipa-scan）。純 CPU。"""
    out: list[dict] = []
    for h in scan_variants(text):
        h["via"] = "variant"
        out.append(h)
    if not ipa._PY_OK:                                     # 無拼音引擎 → 跳過 (b)（漢字鄰域召回失效）
        return out
    cache = _index()
    # CAG 正解（模糊層）＝可容未登錄音近的靶；只掃這些，避免寬泛詞過召
    cag_terms = [(c, _flat(c)) for c, _t, is_cag in cache["ipa"] if is_cag]
    seen: set[tuple[int, str]] = set()
    n = len(text)
    for correct, cflat in cag_terms:
        L = len(correct)
        if L < 2:
            continue
        i = 0
        while i + L <= n:
            win = text[i:i + L]
            if not _is_han_run(win):                        # 只比對連續漢字窗（音節逐字對應）
                i += 1
                continue
            wf = _flat(win)
            if wf != cflat and ipa.word_distance(win, correct) <= fuzzy_thr:
                key = (i, correct)
                if key not in seen:
                    seen.add(key)
                    out.append({"surface": win, "correct": correct, "start": i, "via": "ipa-scan"})
            i += 1
    return out


def _is_han_run(s: str) -> bool:
    """整串皆漢字（IPA 鄰域窗只在連續漢字上滑，非漢字不逐字比）。"""
    from . import langtag
    return bool(s) and all(langtag.char_script(c) == "Han" for c in s)


def stats() -> dict:
    """供驗證/前端：索引大小。"""
    idx = _index()
    ipa_list = idx.get("ipa", [])
    return {"ipa_terms": len(ipa_list), "cag_terms": sum(1 for _c, _t, g in ipa_list if g),
            "ipa_index": _IPA_INDEX, "fuzzy": _IPA_FUZZY, "exact": _IPA_EXACT,
            "knowledge_terms": idx.get("know", 0), "knowledge_seed": _KNOW,
            "variants": len(idx.get("variants", {})), "ambiguous": len(idx.get("ambiguous", {})),
            "refterms_dir": _REFTERMS_DIR, "enabled": ipa._PY_OK}


def invalidate() -> None:
    """字典 commit / 放入 refterms 檔後可主動清快取（下次查詢重建）。"""
    global _cache
    with _lock:
        _cache = None
