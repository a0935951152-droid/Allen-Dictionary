"""收斂式字典 API（架構 §6）。節點皆為 JSON 資源：GET 瀏覽、POST/PATCH 編輯。

排序原則（§7）：生成→收斂→篩選→驗證(接地)→分流→彙整→回灌。
本階段：收斂/接地/分流/彙整已實作；生成(STT/TTT)、IPA/TTS sync 留接口待下載模型。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from typing import Optional

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import clients
from .config import settings
from .engine import knowledge, review, vault_graph
from .engine import know_index, lexicon, phonetic_index, wiki_index
from .engine.variants import flatten
from .schemas import (
    BatchNode,
    Classification,
    DecisionInfo,
    Grounding,
    HistoryItem,
    Occurrence,
    SpanNode,
    TermEntry,
    Version,
)
from .storage import store

app = FastAPI(title="收斂式字典 API", version="0.1.0")


# ── 健康檢查 ───────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"api": True, "services": await clients.health()}


# ── 批次 CRUD ──────────────────────────────────────────────────
@app.get("/api/batches")
def list_batches():
    return {"batches": store.list_batches()}


@app.post("/api/batches", status_code=201)
def create_batch(batch: BatchNode):
    if store.get_batch(batch.batch_id):
        raise HTTPException(409, "batch_id 已存在")
    store.save_batch(batch)
    return batch


def _require(batch_id: str) -> BatchNode:
    b = store.get_batch(batch_id)
    if not b:
        raise HTTPException(404, "batch 不存在")
    return b


@app.get("/api/batches/{batch_id}")
def get_batch(batch_id: str):
    return _require(batch_id)


@app.delete("/api/batches/{batch_id}", status_code=204)
def delete_batch(batch_id: str):
    import os
    path = store._batch_path(batch_id)
    if os.path.exists(path):
        os.remove(path)


# ── 〔來源 / 生成〕────────────────────────────────────────────
@app.get("/api/batches/{batch_id}/sources")
def get_sources(batch_id: str):
    return _require(batch_id).sources


@app.put("/api/batches/{batch_id}/sources")
def put_sources(batch_id: str, sources: dict = Body(...)):
    b = _require(batch_id)
    with store.lock(batch_id):
        b.sources = sources
        store.save_batch(b)
    return b.sources


@app.post("/api/batches/{batch_id}/materials")
def add_materials(batch_id: str, material: dict = Body(...)):
    b = _require(batch_id)
    with store.lock(batch_id):
        b.sources.setdefault("materials", []).append(material)
        store.save_batch(b)
    return b.sources["materials"]


# ── 〔站0 入口 / 站1 切分〕──────────────────────
def _parse_srt(text: str) -> str:
    """去掉序號與時間碼，只留字幕文字。"""
    out = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s or s.isdigit() or "-->" in s:
            continue
        out.append(s)
    return "\n".join(out)


# 收斂側只收逐字稿（.txt/.srt）——與知識側（.xlsx/.docx/.pptx/.pdf 乾淨源）嚴格分離（§1.2 鐵律）。
_CONVERGE_EXT = (".txt", ".srt")


def _parse_upload(filename: str, data: bytes) -> str:
    name = (filename or "").lower()
    if not name.endswith(_CONVERGE_EXT):
        raise HTTPException(400, f"收斂側僅支援逐字稿 .txt/.srt（乾淨文件請走知識庫匯入）：{filename}")
    text = data.decode("utf-8", errors="replace")
    return _parse_srt(text) if name.endswith(".srt") else text


async def _apply_ingest(batch_id: str, raw_text: str, filename: str, kind: str) -> dict:
    """站0：存 raw + 不覆寫的 v0 原稿 + 智慧整理 meta。smart_organize 走 breeze（離線可降級）。"""
    meta = await review.smart_organize(raw_text, filename)
    with store.lock(batch_id):
        b = _require(batch_id)
        b.raw = {"text": raw_text, "kind": kind or "transcript", "filename": filename or ""}
        if not any(v.vid == "v0" for v in b.versions):
            b.versions.insert(0, Version(vid="v0", from_="raw", text=raw_text, frozen=True))
        b.meta = {**b.meta, **{k: v for k, v in meta.items() if v}}
        store.save_batch(b)
        out_meta = b.meta
    return {"chars": len(raw_text), "filename": filename, "meta": out_meta}


class IngestIn(BaseModel):
    text: str
    filename: Optional[str] = None
    kind: Optional[str] = None


@app.post("/api/batches/{batch_id}/ingest")
async def ingest_text(batch_id: str, body: IngestIn):
    """站0 入口（貼上）：raw 逐字稿直接送進後端，產 v0 原稿 + 智慧整理。"""
    _require(batch_id)
    return await _apply_ingest(batch_id, body.text, body.filename or "", body.kind or "transcript")


@app.post("/api/batches/{batch_id}/upload")
async def ingest_upload(batch_id: str, file: UploadFile = File(...)):
    """逐字稿匯入（選檔）：僅 .txt/.srt → 解析純文字 → 同 ingest（乾淨文件走知識庫，不在此）。"""
    _require(batch_id)
    data = await file.read()
    raw = _parse_upload(file.filename or "", data)
    return await _apply_ingest(batch_id, raw, file.filename or "", "transcript")


@app.post("/api/batches/{batch_id}/segment")
async def segment_batch(batch_id: str, max_chars: int = 400):
    """切分：raw → offset-aware + bge 邊界切塊（core 連續覆蓋不漏）。整檔不直接跑收斂（§3）。
    core＝下游收斂單位（不重疊）；context 重疊只供切分站手動校正視圖顯示。"""
    b = _require(batch_id)
    raw = (b.raw or {}).get("text") or next((v.text for v in b.versions if v.vid == "v0"), "")
    if not raw:
        raise HTTPException(400, "尚無 raw 逐字稿，請先 /ingest 或 /upload")
    segs = await review.make_segments_offsets(raw, max_chars=max_chars)
    with store.lock(batch_id):
        b = _require(batch_id)
        b.segments = segs
        b.spans = []                      # 重切後 seg_id 全失效，清舊 span（否則孤兒 span 殘留，下游 KeyError）
        store.save_batch(b)
    return {"segments": len(segs),
            "chunks": [{"seg_id": s.seg_id, "idx": s.idx, "chars": len(s.raw_text),
                        "start": s.start, "end": s.end} for s in segs]}


class ReviewIn(BaseModel):
    seg_id: Optional[str] = None           # 指定單一 chunk；省略＝全部


@app.post("/api/batches/{batch_id}/review")
async def review_batch_ep(batch_id: str, body: Optional[ReviewIn] = None):
    """偵測（判定側）＝**ner 抓 span → IPA 對 CAG 索引比對**（detect→retrieve，純 CPU）。
    命中音近 CAG 正解（且本身非正解）→ 產「原詞→建議正解」span，標『存疑』待 ground_context/判官確認。
    （與 `/ground_context` 內建的偵測同源；此端點供單獨重跑偵測。）"""
    body = body or ReviewIn()
    b = _require(batch_id)
    if not b.segments:
        raise HTTPException(400, "尚無 chunk，請先 /segment")
    targets = [s for s in b.segments if (body.seg_id is None or s.seg_id == body.seg_id)]
    if not targets:
        raise HTTPException(404, "seg_id 不存在")
    results = dict(await review.detect_many(targets))     # seg_id → [span]（純 CPU：ner + IPA）
    with store.lock(batch_id):
        b = _require(batch_id)
        store.snapshot_spans(b)                           # 跑前快照（§3 退回）
        seg_by = {s.seg_id: s for s in b.segments}
        tids = set(results)
        b.spans = [sp for sp in b.spans if sp.seg_id not in tids]   # 換掉目標 chunk 的舊 span
        n_hit = 0
        for sid, spans in results.items():
            for sp in spans:
                b.spans.append(sp); n_hit += 1
            if sid in seg_by:
                seg_by[sid].span_ids = [sp.span_id for sp in spans]
                seg_by[sid].review = review.chunk_review(spans) or "正確"
        store.save_batch(b)
    return {"reviewed": len(targets), "suggested_spans": n_hit, "mode": "detect_retrieve_ipa"}


@app.patch("/api/batches/{batch_id}/meta")
def patch_meta(batch_id: str, meta: dict = Body(...)):
    """站0 智慧整理欄位人工修正（meeting_date/produced_date/origin…）。"""
    with store.lock(batch_id):
        b = _require(batch_id)
        b.meta = {**b.meta, **meta}
        store.save_batch(b)
    return b.meta


# ── 〔span 瀏覽 / 人工裁決〕───────────────────────────────────
@app.get("/api/batches/{batch_id}/spans")
def get_spans(batch_id: str):
    b = _require(batch_id)
    return [s.model_dump(by_alias=True) for s in b.spans]


def _find_span(b: BatchNode, sid: str) -> SpanNode:
    for s in b.spans:
        if s.span_id == sid:
            return s
    raise HTTPException(404, "span 不存在")


@app.get("/api/batches/{batch_id}/spans/{sid}")
def get_span(batch_id: str, sid: str):
    s = _find_span(_require(batch_id), sid)
    return s.model_dump(by_alias=True)


class SpanPatch(BaseModel):
    value: Optional[str] = None             # 人工改字 → history(by=human)
    is_proper_noun: Optional[bool] = None
    decision_to: Optional[str] = None       # 人工裁決
    correct: Optional[str] = None
    category: Optional[str] = None          # 改類別（§3 r1）
    review: Optional[str] = None            # 改審查判定 正確/存疑/錯字（§3 r1）


@app.patch("/api/batches/{batch_id}/spans/{sid}")
async def patch_span(batch_id: str, sid: str, patch: SpanPatch):
    """人工改判/裁決（§6）。人工編輯標 by=human。"""
    b = _require(batch_id)
    with store.lock(batch_id):
        s = _find_span(b, sid)
        if patch.value is not None:
            nxt = (s.history[-1].iter + 1) if s.history else 1
            s.history.append(HistoryItem(iter=nxt, value=patch.value, by="human"))
        if patch.is_proper_noun is not None:
            s.is_proper_noun = patch.is_proper_noun
        if patch.decision_to is not None:
            s.decision.to = patch.decision_to
        if patch.correct is not None:
            s.decision.correct = patch.correct
        if patch.category is not None:
            s.category = patch.category
        if patch.review is not None:
            s.review = patch.review  # type: ignore[assignment]
        store.save_batch(b)
    return s.model_dump(by_alias=True)


# ── 〔匯流 / 回灌〕────────────────────────────────────────────
@app.post("/api/batches/{batch_id}/commit")
def commit(batch_id: str):
    """分流結果併入字典（§9）：去重 / 累積 variant / 信心↑。
    只併 decision=auto（建議收錄）；human/regex/pending 留待人工。"""
    b = _require(batch_id)
    terms = store.load_dict()
    merged = 0
    for s in b.spans:
        if s.decision.to != "auto" or not s.decision.correct:
            continue
        correct = s.decision.correct
        tid = f"t_{flatten(correct)}"
        # 原詞＝history 第一個 model 版本（s.current 在判官 _fix 後已被改成正解，拿它會永遠等於 correct
        # → variants 永遠不累積、occurrence.wrong 全 None＝回灌斷鏈）
        wrong = next((h.value for h in s.history if h.by == "model"), None) or (s.current or correct)
        entry = terms.get(tid) or TermEntry(term_id=tid, correct=correct)
        # A5 守門：通用詞不進 variants（per-context 的判定不得壓扁成無條件變體）；occurrence 照記留審計
        if wrong != correct and wrong not in entry.variants and not lexicon.is_common_word(wrong):
            entry.variants.append(wrong)
        occ_wrong = wrong if wrong != correct else None
        if not any(o.batch_id == batch_id and o.wrong == occ_wrong and o.context == s.context
                   for o in entry.occurrences):     # 重複 commit 不灌重複佐證
            entry.occurrences.append(Occurrence(
                batch_id=batch_id,
                wrong=occ_wrong,
                context=s.context,
                confidence=1.0,                 # 判官確認的 auto 決策才進此（decision.to=auto）
                grounding=s.grounding.url,
            ))
        if b.domain and b.domain not in entry.domains:
            entry.domains.append(b.domain)
        # 信心隨佐證的 **distinct 批次數** 提升（§9.4；A5：同批 N 個 span 非 N 份獨立佐證）
        batches_seen = {o.batch_id for o in entry.occurrences if o.batch_id}
        entry.confidence = round(min(0.5 + 0.1 * len(batches_seen), 0.99), 4)
        entry.updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        terms[tid] = entry
        merged += 1
    store.save_dict(terms)
    return {"merged": merged, "terms_total": len(terms)}


@app.get("/api/dictionary/terms")
def get_terms():
    return {k: v.model_dump(by_alias=True) for k, v in store.load_dict().items()}


class TermPatch(BaseModel):
    correct: Optional[str] = None
    ipa: Optional[str] = None
    variants: Optional[list[str]] = None
    confidence: Optional[float] = None


@app.patch("/api/dictionary/terms/{term_id}")
def patch_term(term_id: str, patch: TermPatch):
    """字典收錄由使用者親自判斷（§10.4）：人工調整 correct/ipa/variants/confidence。"""
    terms = store.load_dict()
    if term_id not in terms:
        raise HTTPException(404, "term 不存在")
    t = terms[term_id]
    for f in ("correct", "ipa", "variants", "confidence"):
        v = getattr(patch, f)
        if v is not None:
            setattr(t, f, v)
    t.updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    terms[term_id] = t
    store.save_dict(terms)
    return t.model_dump(by_alias=True)


@app.post("/api/dictionary/sync")
def sync():
    """彙整後重生三套系統（§9.5）。本階段：② regex 規則就地生成；
    ① IPA(g2pW) 與 ③ TTS(CosyVoice) 容器待下載模型後接上，先回報 pending。"""
    terms = store.load_dict()
    corrects = {flatten(t.correct) for t in terms.values() if t.correct}
    regex_rules, contextual = [], []
    for t in terms.values():
        for variant in t.variants:
            rule = {"pattern": variant, "replace": t.correct, "term_id": t.term_id}
            # A5 守門：通用詞變體／本身是字典正解的變體 → 不得吐成無條件全域替換，另列 contextual
            if lexicon.is_common_word(variant) or flatten(variant) in corrects:
                contextual.append(rule)
            else:
                regex_rules.append(rule)
    out = {
        "regex": regex_rules,                       # ② 立即可用（無條件替換安全的子集）
        "contextual": contextual,                   # 需語境判定，下游**不得**盲替換
        "ipa": {"status": "pending", "note": "g2pW 容器待納入封閉建構"},
        "tts": {"status": "pending", "note": "CosyVoice2 容器待納入封閉建構"},
        "terms_total": len(terms),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    # 落檔給下游（糾正側輸出，data/correct/sync.json；原子寫）
    path = os.path.join(settings.data_dir, "correct", "sync.json")
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        out["file"] = path
    except Exception:
        out["file"] = None                          # 落檔失敗不擋回應（降級不壞）
    return out


# ── 〔ops 佇列〕前端一鍵 build/judge → 主機端 dic agent 執行（api 容器無 docker 權限＝封閉設計）──
_OPS_DIR = os.path.join(settings.data_dir, "ops")


def _ops_read(name: str) -> dict:
    try:
        with open(os.path.join(_OPS_DIR, name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _agent_alive() -> bool:
    p = os.path.join(_OPS_DIR, "agent.alive")
    return os.path.exists(p) and (time.time() - os.path.getmtime(p) < 15)


@app.post("/api/ops/{op}")
def ops_request(op: str, body: dict = Body(default={})):
    """排入獨佔窗口操作（build=知識庫抽取一輪 / judge=判官批次）。單工：進行中一律 409。
    寫 request.json 給主機端 `dic agent`（收GPU→載Gemma→執行→換回收斂棧），前端輪詢 /api/ops/status。"""
    if op not in ("build", "judge"):
        raise HTTPException(404, "未知操作（只支援 build / judge）")
    os.makedirs(_OPS_DIR, exist_ok=True)
    if not _agent_alive():
        raise HTTPException(503, "ops 代理未啟動：主機執行 dic up（自動帶起）或 dic agent")
    busy = (_ops_read("status.json").get("state") == "running"
            or os.path.exists(os.path.join(_OPS_DIR, "request.json"))
            or os.path.exists(os.path.join(_OPS_DIR, "request.run")))
    if busy:
        raise HTTPException(409, "已有獨佔窗口操作進行中（GPU 單卡序列化）")
    req = {"id": f"{op}_{uuid.uuid4().hex[:8]}", "op": op, "clear": bool(body.get("clear")),
           "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    # 先重置 status 為 queued——否則輪詢方在代理認領前讀到「上一次操作的 done」會誤判秒完成
    init = {"id": req["id"], "op": op, "state": "queued", "step": 0, "steps": 5,
            "phase": "已排入佇列，等待代理認領…", "log": []}
    for name, payload in (("status.json", init), ("request.json", req)):
        tmp = os.path.join(_OPS_DIR, name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, os.path.join(_OPS_DIR, name))
    return {"queued": True, "id": req["id"]}


@app.get("/api/ops/status")
def ops_status():
    """獨佔窗口進度（前端輪詢）：agent 心跳 + 目前/最後一次操作的 state/step/phase/log。"""
    st = _ops_read("status.json")
    return {"agent": _agent_alive(),
            "pending": os.path.exists(os.path.join(_OPS_DIR, "request.json")), **st}


@app.get("/api/pipeline/params")
def pipeline_params():
    """管線參數視覺化（**唯讀**）：各階段實際生效的參數（引擎實讀 `settings.*`，非 config 帳面值）
    ＋每階段的公式＋現場索引統計。調整走 .env → `docker compose up -d api`，不開寫入端點。"""
    s = settings
    P = lambda key, env, val, desc: {"key": key, "env": env, "value": val, "desc": desc}
    stages = [
        {"id": "detect", "name": "① 偵測", "desc": "三偵測器找可疑詞（皆標存疑，不改字）：登錄變體直掃 → CAG IPA 滑窗 → NER+IPA。偵測前剝 ⚠️/(?…) 註記。",
         "params": [
             P("ipa_fuzzy", "IPA_FUZZY", s.ipa_fuzzy, "IPA 滑窗提名門檻（word_distance ≤ 此值才成 span）"),
             P("phon_query_limit", "PHON_QUERY_LIMIT", s.phon_query_limit, "每詞最多召回幾個音近候選"),
         ],
         "formulas": ["span 來源 via = variant(登錄變體直掃) | ipa-scan(滑窗) | ipa(NER 圈出後比對)"]},
        {"id": "acoustic", "name": "② 聲學軸（音）", "desc": "IPA 音素加權編輯距離：只證明「音像」，不證明「字錯」。變體分級（A1）：通用詞變體＝ambiguous，不必收、不置頂。",
         "params": [
             P("ipa_fuzzy", "IPA_FUZZY", s.ipa_fuzzy, "CAG 模糊召回門檻（專名容未登錄音近）"),
             P("ipa_exact", "IPA_EXACT", s.ipa_exact, "字典/refterms 真同音門檻（寬泛詞只收完全同音）"),
             P("ipa_sub_confuse", "IPA_SUB_CONFUSE", s.ipa_sub_confuse, "混淆類音素替換代價（送氣/捲舌/前後鼻音）"),
             P("ipa_sub_tone", "IPA_SUB_TONE", s.ipa_sub_tone, "聲調 token 代價（A2：真同音須含調全同）"),
         ],
         "formulas": [
             "字距離 = 加權編輯距離(IPA tokens) ÷ 音段數（聲調不進分母）",
             "word_distance = 逐字取 max（防「豆腐」稀釋「沿/腦」差異）",
             "score = 1 − d/門檻（CAG 層）；真同音層 d=0 → 1.0",
             "例：還是↔海蝕 = 0.15（只差聲調）；龍山是↔龍山寺 = 0.67（捲舌/平舌+全異）",
         ]},
        {"id": "semantic", "name": "③ 局部語意軸（意）", "desc": "候選是否被「該詞所在的那一句」語意撐住。兩跳橋：局部句 → 心智圖實體 → 實體預存 wiki（髒稿永不直查 wiki）。",
         "params": [
             P("local_floor", "LOCAL_FLOOR", s.local_floor, "局部句→心智圖餘弦下限（bge 中文基線 0.35–0.47，故配排名制）"),
             P("local_topk", "LOCAL_TOPK", s.local_topk, "排名制：候選須擠進局部句最相似前 k 實體"),
             P("wiki_floor", "WIKI_FLOOR", s.wiki_floor, "實體→預存 wiki 橋接下限"),
             P("mind_retrieve_k", "MIND_RETRIEVE_K", s.mind_retrieve_k, "chunk 接地錨點取幾個心智圖實體"),
         ],
         "formulas": [
             "anchor = top-k 心智圖(term+variants, ≥LOCAL_FLOOR) ∪ 其預存 wiki(title+aliases, ≥WIKI_FLOOR)",
             "rule_hit = 聲學候選 ∈ anchor（音、意兩軸都及格）",
         ]},
        {"id": "judge", "name": "④ gemma 判官（四態）", "desc": "吃兩軸證據＋本段接地錨點＋平行多語 → {abandon, xling}。判官只能確認/否決，不能在規則沒接地時自己造修正。",
         "params": [
             P("judge_concurrency", "JUDGE_CONCURRENCY", s.judge_concurrency, "vLLM 併發判定數"),
             P("common_fpm", "COMMON_FPM", s.common_fpm, "通用詞門檻（jieba 詞頻/百萬詞）：原詞≥此值 → 永不 auto、封頂人工"),
         ],
         "formulas": [
             "四態 = rule_hit × abandon：命中+不拋棄=修(auto)；命中+拋棄/不命中+拋棄=維持；不命中+不拋棄(跨語救回)=人工",
             "沒跨語命中就拋棄（規則沒撐時 gemma 純語境臆測不足以救）",
             "is_common_word(原詞) → 即使判「修」也降級第④態人工（A1/A3）",
         ]},
        {"id": "commit", "name": "⑤ 回灌 / 輸出", "desc": "auto 決策併入字典；sync 輸出替換規則給下游。守門（A5）：per-context 的結論不得壓扁成無條件規則。",
         "params": [
             P("common_fpm", "COMMON_FPM", s.common_fpm, "通用詞不進 entry.variants、不吐 regex（列 contextual）"),
         ],
         "formulas": [
             "confidence = min(0.5 + 0.1 × distinct 批次數, 0.99)（同批 N 個 span 只算 1 份佐證）",
             "sync = regex(安全子集，可盲替換) + contextual(通用詞/字典詞變體，下游不得盲替換)",
         ]},
    ]
    return {"stages": stages,
            "live": {"phonetic": phonetic_index.stats(), "know": know_index.stats()},
            "note": "唯讀顯示。調整：改 .env → docker compose up -d api（引擎已實讀 settings.*）。"}


# ── 分類（§3 分類注入；具名 LLM 提示詞，跑前手選注入）─────────
@app.get("/api/classifications")
def list_classifications():
    return {k: v.model_dump() for k, v in store.load_classifications().items()}


class ClassIn(BaseModel):
    name: str
    prompt: str = ""


@app.post("/api/classifications", status_code=201)
def create_classification(body: ClassIn):
    items = store.load_classifications()
    cid = "cls_" + uuid.uuid4().hex[:8]
    c = Classification(class_id=cid, name=body.name, prompt=body.prompt)
    items[cid] = c
    store.save_classifications(items)
    return c.model_dump()


class ClassPatch(BaseModel):
    name: Optional[str] = None
    prompt: Optional[str] = None


@app.patch("/api/classifications/{cid}")
def patch_classification(cid: str, body: ClassPatch):
    items = store.load_classifications()
    if cid not in items:
        raise HTTPException(404, "分類不存在")
    c = items[cid]
    if body.name is not None:
        c.name = body.name
    if body.prompt is not None:
        c.prompt = body.prompt
    items[cid] = c
    store.save_classifications(items)
    return c.model_dump()


@app.delete("/api/classifications/{cid}", status_code=204)
def delete_classification(cid: str):
    items = store.load_classifications()
    if cid in items:
        del items[cid]
        store.save_classifications(items)


# ── 知識庫心智圖（Obsidian vault → 可瀏覽 graph 後端，§4）─────────
@app.get("/api/knowledge/graph")
def knowledge_graph(category: Optional[str] = None, source: Optional[str] = None,
                    min_conf: float = 0.0, q: Optional[str] = None, limit: int = 400):
    """vault/terms 的心智圖 {nodes,edges,stats}（過濾：category/source/min_conf/q/limit）。
    前端 /graph.html 用 force-directed 瀏覽；不依賴 Obsidian。"""
    return vault_graph.graph(category=category, source=source, min_conf=min_conf, q=q, limit=limit)


@app.get("/api/knowledge/categories")
def knowledge_categories():
    """各領域/類別詞數（供心智圖過濾下拉）。"""
    return {"categories": vault_graph.categories()}


class KBuildIn(BaseModel):
    domain: str = "speakin"
    use_llm: bool = True            # 切 chunk 餵 LLM 抽詞+語意（GraphRAG 精度層）
    llm_cap: int = 40               # 每檔最多 chunk 數（控總 LLM 呼叫）
    gleanings: int = 1              # GraphRAG gleaning 補抽趟數（拉高召回）
    incremental: bool = True        # 只重抽新/改動檔（內容 hash 快取）；False＝全重抽
    clear: bool = False             # 重建前清空 vault/cag/manifest


@app.post("/api/knowledge/build")
async def knowledge_build(body: Optional[KBuildIn] = None):
    """後端建/更新知識庫（§3）：sources/ → Obsidian vault + CAG。**CAG 只由此端點修改**。
    glossary 對照表＝gold(T1)；乾淨文件切 chunk 餵 LLM 抽『易錯詞+領域術語』；
    證據分層 → CAG 只收 T1(對照表) + T2(跨檔多次)，T3 單次留 vault。"""
    body = body or KBuildIn()
    if body.clear:
        knowledge.clear_outputs()
    return await knowledge.build(domain=body.domain, use_llm=body.use_llm,
                                 llm_cap=body.llm_cap, gleanings=body.gleanings,
                                 incremental=body.incremental)


@app.post("/api/knowledge/relabel")
def knowledge_relabel(domain: str = "speakin"):
    """免重抽：把現有 vault 類別併回 8 類實體 taxonomy，重生 domains/cag（§11 類別正規化）。"""
    return knowledge.relabel(domain=domain)


@app.get("/api/knowledge/sources")
def knowledge_sources():
    """列 sources/ 全檔 + 抽取狀態（extracted/changed/new/skip；對 manifest 內容 hash 比對）+ 每檔抽取/排除決策。
    前端「每次要抽取的檔案」面板用：已抽取(extracted)預設收合，可拉開觀察；每檔可手動選抽取/排除。"""
    return knowledge.list_sources()


class KDecideIn(BaseModel):
    rel: str                        # 來源檔相對路徑（list_sources 回的 rel）
    decision: str                   # extract（抽取）| exclude（排除）| auto（清除覆寫，回自動判斷）


@app.post("/api/knowledge/decide")
def knowledge_decide(body: KDecideIn):
    """逐檔設定抽取/排除（凌駕自動判斷）。下次 build 生效；不重抽既有快取。"""
    try:
        return knowledge.set_decision(body.rel, body.decision)
    except ValueError as ex:
        raise HTTPException(400, str(ex))


class KMoveIn(BaseModel):
    rel: str                        # 來源檔相對路徑
    action: str                     # exclude（搬進 _excluded/）| extract（搬回根目錄）


@app.post("/api/knowledge/move")
def knowledge_move(body: KMoveIn):
    """排除/抽取＝實際搬移檔案（排除→_excluded/、抽取→根目錄）。物理位置即決策。"""
    try:
        return knowledge.move_source(body.rel, body.action)
    except ValueError as ex:
        raise HTTPException(400, str(ex))


@app.post("/api/knowledge/upload")
async def knowledge_upload(file: UploadFile = File(...), subdir: str = ""):
    """前端上傳乾淨來源檔進 sources/（可選 subdir＝講者/場次）。不自動 build，由按鈕觸發。"""
    data = await file.read()
    try:
        return knowledge.save_upload(file.filename or "", data, subdir=subdir)
    except ValueError as ex:
        raise HTTPException(400, str(ex))


@app.get("/api/knowledge/stats")
def knowledge_stats():
    """知識庫儀表板統計（總數/類別/tier/來源/特殊詞/CAG 大小；讀 vault frontmatter，無 LLM）。"""
    return knowledge.stats()


# ── 〔CAG 變體人工管理〕（前端直接改/刪/增變體，治理人工核可）─────────────
@app.get("/api/cag/variants")
def cag_list(q: str = "", only_special: bool = True):
    """列 CAG 詞 + 變體（變體多的排前，方便找雜訊清理）。"""
    return knowledge.list_cag(q=q, only_special=only_special)


class VariantsIn(BaseModel):
    variants: list[str] = []                # 覆蓋式：傳完整變體清單（增/刪/改都用這個）


async def _resync_mind():
    """校正後重嵌心智圖（接地用 cards.json；增量只重嵌變動詞）。best-effort、背景跑。
    embed 服務離線（如序列化 GPU build 模式 TEI 停機）→ 直接跳過，待之後 build 補嵌，
    不讓變體/校正的 PUT 卡在 embed timeout。糾正側（聲學索引）已在 set_* 同步刷好、不受影響。"""
    try:
        if not await clients.embed_alive():
            return
        await know_index.build()
    except Exception:
        pass


def _resync_mind_bg():
    """背景觸發重嵌（fire-and-forget）：PUT/PATCH 立即回應，嵌入慢慢補。"""
    asyncio.create_task(_resync_mind())


@app.put("/api/cag/variants/{term}")
async def cag_set_variants(term: str, body: VariantsIn):
    """設定某 CAG 詞的變體（覆蓋）：寫回 vault md（locked，build 不沖）+ 刷聲學索引 + 同步心智圖嵌入。"""
    res = knowledge.set_variants(term, body.variants)
    if res.get("error"):
        raise HTTPException(404, res["error"])
    _resync_mind_bg()
    return res


class TermPatchIn(BaseModel):
    is_special: Optional[bool] = None       # False＝標「不收」移出 CAG；True＝收回
    correct: Optional[str] = None           # 改正解（重命名）


@app.patch("/api/cag/terms/{term}")
async def cag_set_term(term: str, body: TermPatchIn):
    """人工校正 CAG 詞（鎖定）：標不收（is_special=false 移出 CAG）/ 改正解（correct 重命名）。"""
    res = knowledge.set_term(term, is_special=body.is_special, correct=body.correct)
    if res.get("error"):
        raise HTTPException(404, res["error"])
    _resync_mind_bg()
    return res


@app.delete("/api/cag/terms/{term}")
async def cag_delete_term(term: str):
    """刪整個心智圖節點（注意：來源仍在時 build 可能重抽；持久排除建議用『不收』）。"""
    res = knowledge.set_term(term, delete=True)
    if res.get("error"):
        raise HTTPException(404, res["error"])
    _resync_mind_bg()
    return res


# ── 〔接地大庫：wiki 攝取 / 索引 / 心智圖連結〕（§4.1 第二次 hybrid）─────────
@app.post("/api/wiki/ingest")
def wiki_ingest_ep():
    """解析 data/wiki/raw 的 zhwiki dump → entries.jsonl（標題+別名；純 CPU，數分）。"""
    from .engine import wiki_ingest
    return wiki_ingest.build()


@app.post("/api/wiki/reindex")
async def wiki_reindex_ep():
    """entries.jsonl → bge-m3 嵌入 + hnswlib 索引（GPU，長跑）。建議改用 CLI 背景跑大庫。"""
    return await wiki_index.build()


@app.get("/api/wiki/stats")
def wiki_stats_ep():
    return wiki_index.stats()


@app.get("/api/wiki/search")
async def wiki_search_ep(q: str, k: int = 8):
    """以 q（通常＝心智圖實體 term）bge 檢索 wiki 大庫 → 回 [{title,aliases,score}]。
    心智圖前端點節點即時查它 grounded 的 wiki 語境（§4.1 心智圖→wiki 橋）。"""
    return await wiki_index.search(q, k=k)


class WikiLinksIn(BaseModel):
    terms: list[str] = []
    k: int = 2
    floor: float = 0.0                      # 分數下限（過濾弱對應，自創/縮寫詞）


@app.post("/api/wiki/links")
async def wiki_links_ep(body: WikiLinksIn):
    """批次：一批心智圖實體 → 各自 grounded 的 top wiki（前端『實體→wiki 二部圖』用）。
    回 {term: [{title,aliases,score}]}。上限 120 個實體（避免一次太多 hnsw 查詢）。"""
    out: dict[str, list] = {}
    for t in body.terms[:120]:
        hits = await wiki_index.search(t, k=body.k)
        out[t] = [h for h in hits if h.get("score", 0) >= body.floor]
    return out


@app.get("/api/wiki/mind_links")
def wiki_mind_links_ep(n: int = 20, floor: float = 0.5, q: str = ""):
    """wiki 接地頁：讀**預存**的 card→wiki 連結（q 篩選 term，否則前 n 個實體二部圖）。連結用 card
    語義 (term｜gloss｜names｜變體) 檢索、隨 card 增量更新（embed_mind／人工 CRUD 都會連帶刷新）。
    秒回，不實時 embed。回 {entities:[…], links:{term:[{title,aliases,score}]}}。"""
    return know_index.mind_links(n=n, floor=floor, q=q)


@app.post("/api/knowledge/embed_mind")
async def knowledge_embed_mind():
    """嵌入心智圖實體向量（供接地時 chunk→心智圖 檢索；增量，只嵌新/變動 term）。
    註：wiki 不在此連結——wiki 由 chunk 於 /ground_context 直接檢索當語境（§4.1）。"""
    return await know_index.build()


@app.get("/api/knowledge/mind_stats")
def knowledge_mind_stats():
    return know_index.stats()


# ── 〔接地語境 / 粗修〕（§4.1 步驟①②；先接地再粗修）─────────────────────
@app.post("/api/batches/{batch_id}/ground_context")
async def locate_ep(batch_id: str):
    """階段①「局部語意語音命中」（取代接地）：偵測可疑詞 → 對每個詞產**兩條規則證據**：
    聲學(IPA→CAG 音近候選+距離) ＋ 局部語意(候選實體 vs **該詞局部句**向量餘弦，擋文件主題滲入)。
    `rule_hit`＝聲學有候選且局部語意撐住。不改字，交階段② gemma 判斷。"""
    from .engine import reports
    b = _require(batch_id)
    if not b.segments:
        raise HTTPException(400, "尚無 chunk，請先 /segment")
    seg_by = {s.seg_id: s for s in b.segments}
    det = await review.detect_many(b.segments)            # [(seg_id, spans)]（變體掃描+ner+IPA）
    pairs, local_texts = [], []                           # (span, surface, seg_id), 局部句
    for sid, spans in det:
        seg = seg_by[sid]
        for sp in spans:
            surface, _ = reports.span_words(sp)
            pairs.append((sp, surface, sid))
            local_texts.append(reports.local_sentence(seg, surface))
    vecs = await clients.embed(local_texts) if local_texts else []
    hits = 0
    for (sp, surface, _sid), lv in zip(pairs, vecs):
        sp.reports = reports.build(surface, lv)           # 聲學候選 + 局部語意撐住者
        cand, hit = reports.rule_hit(sp.reports)
        sp.rule_hit = hit
        sp.grounding = Grounding(checked=True, **{"pass": hit}, candidates=[cand] if cand else [])
        sp.review = "存疑"                                # 待 gemma 判
        if hit:
            hits += 1
    gc_by = {}                                            # §3.2③ 兩源語境錨點（心智圖→wiki 橋）逐段接地
    gcs = await asyncio.gather(*[
        know_index.ground_context(s.core_text or s.raw_text or "") for s in b.segments
    ])
    for s, gc in zip(b.segments, gcs):
        gc_by[s.seg_id] = gc
    with store.lock(batch_id):
        b = _require(batch_id)
        store.snapshot_spans(b)
        by_seg: dict[str, list] = {}
        for sp, _s, sid in pairs:
            by_seg.setdefault(sid, []).append(sp.span_id)
        b.spans = [sp for sp, _s, _sid in pairs]
        for seg in b.segments:
            seg.span_ids = by_seg.get(seg.seg_id, [])
            seg.ground_context = gc_by.get(seg.seg_id, [])
        store.save_batch(b)
    return {"segments": len(b.segments), "spans": len(pairs), "rule_hits": hits}


@app.post("/api/batches/{batch_id}/coarse_fix")
async def judge_ep(batch_id: str):
    """階段②「gemma判斷」（取代粗修+精修）：吃規則證據(聲學+局部語意命中)＋平行多語 → gemma 判
    拋棄?/跨語命中? → 四態落地：①命中+不拋棄=修 ②命中+拋棄/③不命中+拋棄=維持 ④不命中+不拋棄=人工修正。
    需先 ①局部語意語音命中。"""
    from .engine import refine as judge
    b = _require(batch_id)
    seg_by = {s.seg_id: s for s in b.segments}
    # 只判「未判過」的存疑 span：第④態（decision=human）已進人工佇列，重跑不得重擲骰
    # （否則 gemma 再判一次可能翻案，人工佇列悄悄流失；人工項只由 /refine 抽屜處理）
    targets = [s for s in b.spans if s.review == "存疑" and s.decision.to != "human"]
    if not targets:
        return {"fix": 0, "keep": 0, "manual": 0, "candidates": 0}
    counts = await judge.judge_many(targets, seg_by)
    with store.lock(batch_id):
        b = _require(batch_id)
        store.snapshot_spans(b)
        by_id = {s.span_id: s for s in targets}
        b.spans = [by_id.get(s.span_id, s) for s in b.spans]
        for seg in b.segments:
            sps = [s for s in b.spans if s.seg_id == seg.seg_id]
            if sps:
                seg.review = review.chunk_review(sps) or "正確"
        store.save_batch(b)
    counts["candidates"] = len(targets)
    return counts


@app.post("/api/judge_pending")
async def judge_pending_ep():
    """判官批次（Gemma 獨佔窗口用，搭配 `dic judge`）：掃所有 batch，對 review=='存疑' 的 span
    跑階段② gemma 判斷（複用單批 judge_ep 邏輯）。累積多批 ground_context 後一次判完。回各批彙總。"""
    out = {"batches": 0, "fix": 0, "keep": 0, "manual": 0, "candidates": 0}
    for bid in store.list_batches():
        try:
            counts = await judge_ep(bid)          # 既有階段②：無存疑 span → 回 0，跳過計數
        except HTTPException:
            continue                              # batch 讀不到 → 略過
        if counts.get("candidates", 0):
            out["batches"] += 1
            for k in ("fix", "keep", "manual", "candidates"):
                out[k] += counts.get(k, 0)
    return out


@app.post("/api/batches/{batch_id}/refine")
async def manual_list_ep(batch_id: str):
    """階段③「人工修正」（取代精修）：列出 gemma 判為人工的 span（規則沒撐、但跨語救回 = 第④態）。
    不自動改字，供人工抽屜逐一處理（decision=human、帶建議候選與上下文）。"""
    b = _require(batch_id)
    manual = [s.span_id for s in b.spans if s.decision.to == "human"]
    return {"manual": len(manual), "span_ids": manual}


# ── 前端（§4 可視化）：靜態檔掛在最後，API 路由優先 ─────────────
_static = os.path.join(os.path.dirname(__file__), "static")


class _NoCacheStatic(StaticFiles):
    """前端改版頻繁 → 不讓瀏覽器快取 HTML/JS（避免重整仍跑舊 graph.html）。"""
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp


app.mount("/", _NoCacheStatic(directory=_static, html=True), name="frontend")
