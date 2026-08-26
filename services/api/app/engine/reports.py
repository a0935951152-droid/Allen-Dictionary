"""規則證據層（階段①「局部語意語音命中」）：對每個偵測出的可疑詞，產兩條**規則**證據，
交階段②的 Qwen 判官彙整（Qwen 另做跨語）。信心＝規則命中 與 Qwen 拋棄判斷的一致性。

兩條規則線（互不污染、皆零訓練 CPU/離線）：
- **聲學**：IPA→CAG 音近召回（`phonetic_index.query_scored`）→ 候選 + 距離。**只證明音像，不證明字錯**。
- **局部語意**：候選實體 vec 與「**該詞所在的局部句**」向量餘弦（`know_index.local_support`）。
  用局部句而非整塊 chunk → 擋掉文件主題滲入（「還是」在木構造句裡不會被「海蝕」撐住）。

`rule_hit` ＝ 聲學有候選 **且** 該候選局部語意撐住（≥floor）。命中即「規則有接地」；不命中＝規則沒撐，
要不要改交 Qwen（且只有 Qwen 靠跨語救回時才走人工，見 `judge`）。跨語不在此層（Qwen 直接讀平行文）。
"""
from __future__ import annotations

import re

from ..config import settings
from ..schemas import ReportItem, Segment, SpanNode, SpanReports
from . import know_index, phonetic_index
from .variants import flatten, strip_punct

_SENT = re.compile(r"[^。！？!?；;\n]+")     # 句界切（保留供局部窗口取句）
_LOCAL_FLOOR = settings.local_floor          # chunk→心智圖 餘弦下限（規格 §3.3；env LOCAL_FLOOR）
_WIKI_FLOOR = settings.wiki_floor            # 實體→wiki 相似下限（規格 §3.3；env WIKI_FLOOR）


def span_words(span: SpanNode) -> tuple[str, list[str]]:
    """拆出 (原詞, 偵測建議候選)：原詞＝history 第一個 model 版本；候選＝current + grounding.candidates。"""
    model_vals = [h.value for h in span.history if h.by == "model"]
    original = model_vals[0] if model_vals else (span.current or "")
    cands, seen = [], {flatten(original)}
    for c in [span.current] + ((span.grounding.candidates if span.grounding else []) or []):
        cf = flatten(c) if c else ""
        if c and cf and cf not in seen:
            seen.add(cf); cands.append(c)
    return original, cands


def local_sentence(seg: Segment, surface: str) -> str:
    """取 surface 所在的**局部句**（從含標點的 core_text 切句、找含此詞的那句）。供局部語意命中。"""
    text = seg.core_text or seg.raw_text or ""
    if surface:
        for m in _SENT.finditer(text):
            s = m.group(0)
            if surface in strip_punct(s):
                return s.strip()
    return (text[:80] if text else surface)        # 找不到 → 退回開頭片段


def acoustic(surface: str) -> list[ReportItem]:
    """聲學證據：①登錄變體（確定誤聽，verified，不受 IPA 門檻）置頂 ②IPA→CAG 音近召回。
    **A1 變體分級**：通用詞變體（還是→海蝕 類）不在①（不得滿分必收）——只在②以真實 IPA
    距離提名，證據加註「曾登錄誤聽樣態(需語境確認)」＝條件事實，不是無條件事實。"""
    out: list[ReportItem] = []
    seen: set[str] = set()
    vc = phonetic_index.variant_correct(surface)            # ① 人工登錄變體＝確定誤聽，距離視同 0、必收
    if vc:
        out.append(ReportItem(candidate=vc, score=1.0, evidence="變體(確定誤聽)"))
        seen.add(flatten(vc))
    va = phonetic_index.variant_ambiguous(surface)          # ambiguous：只加註，不必收
    for correct, d, is_cag, thr in phonetic_index.query_scored(surface):   # ② IPA 音近召回
        if flatten(correct) in seen:
            continue
        score = max(0.0, 1.0 - d / thr) if thr > 0 else (1.0 if d <= 0 else 0.0)
        note = "·曾登錄誤聽樣態(需語境確認)" if va and flatten(correct) == flatten(va) else ""
        out.append(ReportItem(candidate=correct, score=round(score, 4),
                              evidence=f"IPA d={d:.3f}{'·CAG' if is_cag else ''}{note}"))
    return out


def semantic_local(cands: list[str], local_vec: list[float], k: int = settings.local_topk) -> list[ReportItem]:
    """局部語意證據（規格 §3.2 心智圖→wiki 橋 + §5.1 anchor）：
    ① 局部句 → top-k 心智圖實體（排名制，≥mind_floor）；② 每實體橋接其**預存 wiki**（title+aliases，≥wiki_floor）；
    ③ anchor = mind(term+variants) ∪ wiki(title+aliases)。候選 ∈ anchor 即撐住。
    wiki 把語意範圍從講者窄庫**擴大到客觀大庫**——候選即使不在心智圖、只要在相關實體的 wiki 也算撐。"""
    anchor: dict[str, tuple[float, str]] = {}                  # flatten(字) → (分數, 來源)
    for term, s in know_index.top_terms(local_vec, k):        # ① 相關心智圖實體（排名制擋主題滲入）
        if s < _LOCAL_FLOOR:
            continue
        c = know_index.card_of(term) or {}
        for t in [term] + (c.get("variants") or []):          # ③ mind 側：term + variants
            anchor.setdefault(flatten(t), (round(s, 4), f"心智圖:{term}"))
        for w in c.get("wiki", []):                           # ② wiki 橋接 → ③ wiki 側：title + aliases
            if w.get("score", 0) >= _WIKI_FLOOR:
                for nm in [w.get("title", "")] + (w.get("aliases") or []):
                    if nm:
                        anchor.setdefault(flatten(nm),
                                          (round(float(w["score"]), 4), f"wiki:{w.get('title')}←{term}"))
    out: list[ReportItem] = []
    for c in cands:                                           # **並行軸**：每個聲學候選都給語意讀數，不過濾隱藏
        hit = anchor.get(flatten(c))
        if hit:
            out.append(ReportItem(candidate=c, score=hit[0], evidence=f"局部語意 {hit[1]} {hit[0]:.2f}"))
        else:
            out.append(ReportItem(candidate=c, score=0.0, evidence="局部語意 未撐"))   # 象限② 顯式顯示
    return out


def build(surface: str, local_vec: list[float]) -> SpanReports:
    """組規則證據：聲學候選 + 局部語意撐住者。crosslingual 留空（Qwen 讀平行文，不在規則層）。"""
    ac = acoustic(surface)
    se = semantic_local([r.candidate for r in ac], local_vec)
    return SpanReports(acoustic=ac, semantic=se)


def rule_hit(reports: SpanReports) -> tuple[str | None, bool]:
    """規則命中：取「聲學召回 且 局部語意撐住」的最佳候選 → (候選, True)。
    無語意撐 → 回 (聲學最佳, False)＝有候選但規則沒接地，交 Qwen。皆無 → (None, False)。"""
    sem = {flatten(r.candidate) for r in reports.semantic if r.score > 0}   # 只認**真撐**（未撐 score=0 不算）
    for r in reports.acoustic:                      # acoustic 已按距離排序，最近的優先
        if flatten(r.candidate) in sem:
            return r.candidate, True
    return (reports.acoustic[0].candidate if reports.acoustic else None), False


def render_line(items: list[ReportItem]) -> str:
    """證據 → 給 Qwen 的精簡單行。"""
    return " ".join(f"{it.candidate}({it.evidence})" for it in items) or "（無）"
