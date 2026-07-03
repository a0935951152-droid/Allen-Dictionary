"""判定側 切分 + 偵測（便宜先跑、純 CPU）。

職責：
- make_segments_offsets：切分站主切塊（offset-aware + bge 邊界 + core/context）。
- smart_organize：從 raw 智慧整理 會議/產出日期、來源（judge_chat，離線可降級）。
- review_detect_segment / detect_many：① 變體掃描（已登錄 CAG 變體）＋ ② NER 定位器 + IPA 對 CAG 撈音近正解
  → 產「原詞→建議正解」span（皆標『存疑』）。認對交 `ground_context`（兩軸證據）→ 判官拍板。
- chunk_review：彙整一塊的 span 判定（錯字>存疑>正確）。
"""
from __future__ import annotations

import asyncio
import json
import re

from .. import clients
from ..schemas import Grounding, HistoryItem, Segment, SpanNode
from . import phonetic_index
from .variants import flatten

REVIEW_CONCURRENCY = 8                    # 偵測併發（純 CPU：ner + IPA）
_SKIP_TYPES = {"CARDINAL", "DATE", "MONEY", "ORDINAL", "PERCENT", "QUANTITY", "TIME"}
# 逐字稿人工註記（⚠️(?風化) 之類）會切斷實體字串 → 偵測前剝離，還原乾淨連續字面。
_DETECT_MARKERS = re.compile(r"⚠️?\s*|[（(]\?[^）)]*[）)]")


def _clean_for_detect(text: str) -> str:
    """剝離逐字稿 inline 註記（⚠️、(?…)、（?…）），讓變體/詞表直掃比得到連續字串。"""
    return _DETECT_MARKERS.sub("", text or "")


async def make_segments_offsets(raw_text: str, max_chars: int = 400) -> list[Segment]:
    """切分站主切塊（offset-aware + bge 邊界 + core/context）：core 連續覆蓋不漏、下游只吃 core；
    context 重疊只存欄位供手動校正視圖顯示，不進收斂。bge 缺服務時自動降級規則貪婪。"""
    from .chunk import make_offset_chunks
    chunks = await make_offset_chunks(raw_text, hi=max_chars)
    out: list[Segment] = []
    for i, c in enumerate(chunks):
        out.append(Segment(seg_id=f"c_{i:03d}", idx=i, raw_text=c["zh"],
                           normalized=flatten(c["zh"]), ref=c.get("ref", ""),
                           langs=c.get("langs", {}),
                           start=c["start"], end=c["end"], core_text=c["core_text"],
                           ctx_before=c["ctx_before"], ctx_after=c["ctx_after"]))
    return out


def _extract_json(text: str):
    """從 breeze 回覆抽出第一段 JSON（容忍 ```json 包裹/前後雜訊）。失敗回 None。"""
    if not text:
        return None
    m = re.search(r"\{.*\}|\[.*\]", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


async def smart_organize(raw_text: str, filename: str = "") -> dict:
    """從 raw 推斷 meeting_date/produced_date/origin（智慧整理）。失敗回空欄位。"""
    head = (raw_text or "")[:1000]
    prompt = (
        "你是會議資料整理員。根據檔名與逐字稿開頭，推斷下列欄位，無法判斷就留空字串。\n"
        f"檔名：{filename or '（無）'}\n逐字稿開頭：\n{head}\n\n"
        '只輸出 JSON：{"meeting_date":"YYYY-MM-DD","produced_date":"YYYY-MM-DD","origin":"來源/出處簡述"}'
    )
    try:
        data = _extract_json(await clients.judge_chat(prompt, max_tokens=160)) or {}
    except Exception:
        data = {}
    return {
        "meeting_date": str(data.get("meeting_date", "") or ""),
        "produced_date": str(data.get("produced_date", "") or ""),
        "origin": str(data.get("origin", "") or ""),
    }


# ── 偵測（判定側主線）：ner 抓 span → IPA 對 CAG 索引比對（純 CPU，detect→retrieve，採 Apple 2409.15353）──
# NER＝可錯的「定位器」（圈出像專名的 span，可以錯）；對 CAG 過 IPA 檢索撈音近正解＝「認對」。
# 不走 breeze 生成/多輪收斂（已移除）；此關純 ner(CPU)+IPA(CPU)，認對交 ground_context/判官。
def _mk_span(seg_id: str, j: int, w: str, suggest: str, typ: str,
             ctx_text: str, cands: list[str], src: str) -> SpanNode:
    ctx = ctx_text.replace(w, f"[{w}]", 1)
    return SpanNode(
        span_id=f"{seg_id}_d{j}", seg_id=seg_id, category=typ,
        context=ctx, is_proper_noun=True, review="存疑",
        history=[HistoryItem(iter=1, value=w, by="model"),
                 HistoryItem(iter=2, value=suggest, by="model")],
        grounding=Grounding(checked=True, **{"pass": True},
                            url=f"{src}://{suggest}", candidates=cands or [suggest]),
    )


async def review_detect_segment(seg: Segment) -> list[SpanNode]:
    """單 chunk 三偵測器 → 對 CAG 比對，產「原詞→建議正解」span（皆標『存疑』待確認）：
    A. **第三偵測器：詞表直掃**（`scan_terms`，繞過 ner、語言無關優先）：登錄變體簡繁直掃（確定誤聽）
       ＋漢字 IPA 鄰域召回（補 ner 沒圈到的未登錄音近，生核化石→生痕化石）。
    B. **ner + IPA**：ner 抓實體（可錯定位器）→ 對 CAG 過 IPA 撈音近正解，補 A 漢字窗外的實體。
    偵測前先剝離逐字稿 inline 註記（⚠️、(?…)），還原連續字串。"""
    dtext = _clean_for_detect(seg.raw_text)            # 剝標記後的乾淨偵測文本
    spans: list[SpanNode] = []
    seen_surface: set[str] = set()
    j = 0

    # A 第三偵測器：詞表直掃（變體簡繁直掃 + 漢字 IPA 鄰域召回，不依賴 ckip）
    # 滑窗×CAG 全表 IPA 距離＝重 CPU 同步計算 → 丟 thread，讓事件迴圈能回 /health（否則整批偵測期間 api 假死）
    hits = await asyncio.to_thread(phonetic_index.scan_terms, dtext)
    for hit in hits:
        w, suggest = hit["surface"], hit["correct"]
        if w in seen_surface or flatten(w) == flatten(suggest):
            continue
        seen_surface.add(w)
        src = "variant" if hit.get("via") == "variant" else "ipa-scan"
        spans.append(_mk_span(seg.seg_id, j, w, suggest, "", dtext, [suggest], src))
        j += 1

    # B ner + IPA（補 A 漢字窗外／非 CAG 靶的實體）
    try:
        ents = await clients.ner(dtext)
    except Exception:
        ents = []
    for e in ents:
        w = (e.get("word") or "").strip()
        typ = e.get("type", "")
        if not w or len(w) < 2 or typ in _SKIP_TYPES or w in seen_surface or w not in dtext:
            continue
        seen_surface.add(w)
        if phonetic_index.is_target(w):                # w 本身已是 CAG 正解 → 對的，不標
            continue
        cands = phonetic_index.query(w)                # 對 CAG：IPA 距離（音近正解）
        if not cands or flatten(cands[0]) == flatten(w):
            continue                                   # 無音近正解 → 不在 CAG／非已知不能錯詞 → 不硬改
        spans.append(_mk_span(seg.seg_id, j, w, cands[0], typ, dtext, cands, "ipa"))
        j += 1
    return spans


async def detect_many(segs: list[Segment], cap: int = REVIEW_CONCURRENCY) -> list[tuple]:
    """併發對多 chunk 跑 detect→retrieve。回 [(seg_id, spans)]。純 CPU（ner + IPA）。"""
    sem = asyncio.Semaphore(cap)

    async def one(seg: Segment):
        async with sem:
            return seg.seg_id, await review_detect_segment(seg)

    return list(await asyncio.gather(*[one(s) for s in segs]))


def chunk_review(spans: list[SpanNode]):
    revs = [s.review for s in spans]
    for level in ("錯字", "存疑", "正確"):
        if level in revs:
            return level
    return None
