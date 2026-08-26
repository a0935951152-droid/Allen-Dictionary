"""Qwen 判斷（階段②，取代舊「粗修+精修」）：吃規則證據（聲學+局部語意命中）＋平行多語句，
Qwen 判兩件事 → 拋棄？／跨語命中？，再依**四態決策**落地。

四態 = (規則語意+聲學命中 rule_hit) × (Qwen 拋棄 abandon)：
  ① 命中 + 不拋棄  → 修（auto，Qwen 確認規則方向）
  ② 命中 + 拋棄    → 維持（auto，Qwen 否決規則）
  ③ 不命中 + 拋棄  → 維持（auto）
  ④ 不命中 + 不拋棄 → 人工修正（規則沒撐、但 Qwen 靠跨語救回 → 交人，永不自動）

治理：Qwen 能確認/否決，但**不能在規則沒接地時自動造修正**（④ 一律走人）。
「沒跨語命中就拋棄」：規則沒撐且跨語也不命中 → 強制 abandon（Qwen 純語境臆測不足以救）。
"""
from __future__ import annotations

import asyncio
import json
import re

from .. import clients
from ..config import settings
from ..schemas import HistoryItem, Segment, SpanNode
from . import lexicon, reports
from .variants import flatten

JUDGE_CONCURRENCY = settings.judge_concurrency   # Qwen 併發（llama.cpp slots；env JUDGE_CONCURRENCY）


def _keep(span: SpanNode, original: str) -> str:
    """維持原詞（②③）：回退 current 到原詞、標正確、不進字典。"""
    nxt = (span.history[-1].iter + 1) if span.history else 1
    if (span.current or "") != original:
        span.history.append(HistoryItem(iter=nxt, value=original, by="model"))
    span.review = "正確"           # type: ignore[assignment]
    span.decision.to = "pending"
    span.decision.correct = None
    return "keep"


def _fix(span: SpanNode, correct: str) -> str:
    """修（①）：改成候選、auto、進字典。"""
    nxt = (span.history[-1].iter + 1) if span.history else 1
    if (span.current or "") != correct:
        span.history.append(HistoryItem(iter=nxt, value=correct, by="model"))
    span.review = "錯字"           # type: ignore[assignment]
    span.decision.to = "auto"
    span.decision.correct = correct
    span.refined = True
    return "fix"


def _manual(span: SpanNode, correct: str) -> str:
    """人工修正（④）：規則沒撐、Qwen 靠跨語救回 → 建議候選但交人，不自動改。"""
    span.review = "存疑"           # type: ignore[assignment]
    span.decision.to = "human"
    span.decision.correct = correct      # 給人參考的建議
    return "manual"


def _render_ground(seg: Segment, cap: int = 12) -> str:
    """把 seg.ground_context（心智圖實體＋橋接 wiki）攤成一行給判官——讓它看得到
    整段講的是什麼主題（木構造句可見「本段零海蝕訊號」），不只憑單句臆測。"""
    out: list[str] = []
    for a in (seg.ground_context or []):
        if a.get("source") == "wiki":
            t = (a.get("title") or "").strip()
            if t:
                out.append(f"維基:{t}")
        else:
            t = (a.get("term") or "").strip()
            if t:
                out.append(t)
        if len(out) >= cap:
            break
    return "、".join(out) if out else "（本段無接地錨點）"


async def judge_span(span: SpanNode, seg: Segment) -> str:
    """單 span：Qwen 雙輸出(abandon/xling) → 四態。回 'fix'|'keep'|'manual'。"""
    original, _ = reports.span_words(span)
    cand = (span.grounding.candidates or [None])[0] if span.grounding else None
    hit = span.rule_hit
    if not cand:
        return _keep(span, original)                         # 無候選 → 維持

    local = reports.local_sentence(seg, original)
    parallel = "\n".join(f"{iso}: {t}" for iso, t in (seg.langs or {}).items()
                         if iso != "cmn" and t)[:600]
    ctx = _render_ground(seg)
    prompt = (
        "任務：判斷語音辨識是否把「原詞」聽錯成候選詞。只輸出 JSON、不要解釋。\n"
        f"原詞：{original}\n候選詞：{cand}\n"
        f"這句：{local}\n"
        f"本段接地錨點：{ctx}\n"
        f"聲學(音近)：{reports.render_line(span.reports.acoustic)}\n"
        f"局部語意：{'命中' if hit else '未命中'} {reports.render_line(span.reports.semantic)}\n"
        + (f"平行多語：\n{parallel}\n" if parallel else "（無平行語言）\n")
        + 'abandon＝是否維持原詞不改。**原詞為常用詞、或原詞在這句語意通順、'
          '或本段接地錨點與候選詞無關 → abandon 應為 true**；\n'
          'xling＝平行多語是否支持改成候選詞（無平行或不支持就 false）。\n'
          '輸出：{"abandon":true/false,"xling":true/false}'
    )
    try:
        raw = await clients.judge_chat(prompt, max_tokens=32, temperature=0.0)
        m = re.search(r"\{.*?\}", raw, re.S)
        data = json.loads(m.group(0)) if m else {}
        abandon = bool(data.get("abandon", True))
        xling = bool(data.get("xling", False))
    except Exception:
        return _keep(span, original)                         # 解析失敗 → 安全維持

    if not hit and not xling:                                # 沒跨語命中就拋棄（規則也沒撐）
        abandon = True
    if hit and not abandon:                                  # ① 修
        if lexicon.is_common_word(original):                 # A1/A3：常用原詞永不 auto → 封頂第④態人工
            return _manual(span, cand)
        return _fix(span, cand)
    if not hit and not abandon:                              # ④ 跨語救回 → 人工
        return _manual(span, cand)
    return _keep(span, original)                             # ②③ 維持


async def judge_many(spans: list[SpanNode], seg_by: dict[str, Segment],
                     cap: int = JUDGE_CONCURRENCY) -> dict[str, int]:
    """併發判斷。回 {fix, keep, manual} 計數。"""
    sem = asyncio.Semaphore(cap)

    async def one(sp: SpanNode) -> str:
        seg = seg_by.get(sp.seg_id)
        if not seg:
            return "keep"
        async with sem:
            return await judge_span(sp, seg)

    res = await asyncio.gather(*[one(s) for s in spans])
    out = {"fix": 0, "keep": 0, "manual": 0}
    for r in res:
        out[r] = out.get(r, 0) + 1
    return out
