"""心智圖檢索索引（§4.1 接地的語境源之一）。

角色（2026-06-24 對齊手繪架構）：**心智圖與 wiki 是兩條平行的「chunk 語境源」**——
接地時 chunk 直接 bge 檢索 wiki 大庫（`wiki_index`）＋心智圖（本模組），兩者都當該 chunk 的
**語境錨點**；錨點再去確認 IPA+NER+CAG 偵測出的 span 修正（信心高才改，見 `coarse`）。

> 註：先前的「心智圖實體→wiki 離線連結」已移除——coined/縮寫詞（AI/AAQW/3A同心分身）在 wiki
> 無對應、硬連產生噪音（AI→醫生遊戲）。wiki 只連 chunk 語境（用 chunk 豐富上下文檢索，噪音大減）。

心智圖小（數百~數千），向量暴力餘弦即可（自帶 `_cosine`），不需 hnswlib。
增量：vault 會長大，只對新/變動 term 重嵌入（term 內容 hash），其餘讀快取。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import threading

from .. import clients
from ..config import settings
from . import vault_graph

_DATA = os.environ.get("DATA_DIR", "/data")
_DIR = os.path.join(_DATA, "index", "know")
_CARDS = os.path.join(_DIR, "cards.json")        # [{term,gloss,category,variants,names,is_special,hash,vec}]

_lock = threading.Lock()
_cache: list[dict] | None = None


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _entity_text(node: dict) -> str:
    """實體 → 待嵌入文字：term + gloss + 別名/外語名（語意接地用）。"""
    parts = [node.get("id", "")]
    if node.get("gloss"):
        parts.append(node["gloss"])
    names = node.get("names") or {}
    if isinstance(names, dict):
        parts += [v for v in names.values() if v]
    parts += node.get("variants") or []
    return "｜".join(p for p in parts if p)


def _hash(node: dict) -> str:
    return hashlib.sha256(_entity_text(node).encode("utf-8")).hexdigest()[:16]


def _term_nodes() -> list[dict]:
    g = vault_graph.graph(limit=100000)
    return [n for n in g.get("nodes", []) if n.get("kind") == "term"]


def _link_wiki(vec, k: int, floor: float) -> list[dict]:
    """用 card 的現成 vec 查 wiki 大庫（免重嵌）→ 預存的 grounded wiki 連結。wiki 未就緒回空。"""
    from . import wiki_index
    return [w for w in wiki_index.search_vec(vec, k=k) if w.get("score", 0) >= floor]


async def _link_cards(cards: list[dict], k: int, floor: float) -> int:
    """整批交 Wiki 常駐 process 冷載/查 HNSW；API process 不載 6GB 索引。"""
    if not cards:
        return 0
    from . import wiki_index
    results = await wiki_index.search_many([card.get("vec") for card in cards], k=k)
    linked = 0
    for card, hits in zip(cards, results):
        card["wiki"] = [w for w in hits if w.get("score", 0) >= floor]
        linked += 1
    return linked


# ── 建置（增量）：嵌入心智圖實體 + 連帶預存 wiki 連結 ─────────────
async def build(batch: int = 128, wiki_k: int = 3, wiki_floor: float = 0.4, **_ignore) -> dict:
    """嵌入 vault 實體向量（供接地時 chunk→心智圖 檢索），並用該 vec 連帶查 wiki 大庫存進 card。
    只處理新/變動 term（hash 比對）：**card 增量 → 連帶更新它的 wiki 連結**（hash 未變則沿用，
    缺 wiki 欄位的舊 card 用既有 vec 補查）。wiki 接地頁直接讀此預存（秒回，不實時 embed）。"""
    # Vault/cards 都在 NAS：同步掃描會讓 event loop 進 D-state；搬到 thread 保持 health/UI 可回應。
    nodes, old_cards = await asyncio.gather(
        asyncio.to_thread(_term_nodes), asyncio.to_thread(_read_cards))
    old = {c["term"]: c for c in old_cards}
    cards: list[dict] = []
    to_embed: list[dict] = []
    to_link: list[dict] = []

    for n in nodes:
        h = _hash(n)
        prev = old.get(n["id"])
        if prev and prev.get("hash") == h and prev.get("vec"):
            if "wiki" not in prev:                # 舊 card 缺 wiki 連結 → 用既有 vec 補查（不重嵌）
                to_link.append(prev)
            cards.append(prev)                    # 語意未變 → 沿用（含已存 wiki）
            continue
        card = {"term": n["id"], "gloss": n.get("gloss", ""), "category": n.get("category", ""),
                "variants": n.get("variants", []), "names": n.get("names", {}),
                "is_special": bool(n.get("is_special")), "tier": n.get("tier", ""),
                "source": n.get("source", ""), "hash": h, "vec": None, "wiki": []}
        to_embed.append((n, card))
        cards.append(card)

    embedded = 0
    for i in range(0, len(to_embed), batch):
        chunk = to_embed[i:i + batch]
        vecs = await clients.embed([_entity_text(n) for n, _ in chunk])
        for (n, card), v in zip(chunk, vecs):
            card["vec"] = v
            to_link.append(card)                    # 稍後整批移到 worker thread 查 wiki
            embedded += 1

    # 冷載 6GB HNSW 與所有 knn_query 都在獨立常駐 process；API GIL/event loop 不受影響。
    relinked = await _link_cards(to_link, wiki_k, wiki_floor)

    await asyncio.to_thread(_write_cards, cards)       # NAS JSON 序列化/原子替換也不阻塞 event loop
    with _lock:
        global _cache
        _cache = None
    return {"terms": len(cards), "embedded": embedded, "wiki_relinked": relinked}


def _write_cards(cards: list[dict]) -> None:
    os.makedirs(_DIR, exist_ok=True)
    tmp = _CARDS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"cards": cards}, f, ensure_ascii=False)
    os.replace(tmp, _CARDS)


def _read_cards() -> list[dict]:
    try:
        with open(_CARDS, encoding="utf-8") as f:
            return json.load(f).get("cards", [])
    except Exception:
        return []


def _cards() -> list[dict]:
    global _cache
    with _lock:
        if _cache is None:
            _cache = _read_cards()
        return _cache


# ── 接地 runtime：chunk → 心智圖語境錨點 ─────────────────────────────
async def retrieve(text: str, k: int = 6, only_special: bool = False,
                   min_score: float = 0.0) -> list[dict]:
    """對 chunk 文字檢索相關心智圖實體＝語境錨點（source=mind）。best-effort，未就緒回空。
    min_score：餘弦低於此值的不回（避免低相關當錨點）。"""
    cards = [c for c in _cards() if c.get("vec") and (not only_special or c.get("is_special"))]
    if not cards or not text:
        return []
    try:
        qv = (await clients.embed([text]))[0]
    except Exception:
        return []
    scored = sorted(((_cosine(qv, c["vec"]), c) for c in cards), key=lambda x: x[0], reverse=True)
    out = []
    for s, c in scored[:k]:
        if s < min_score:
            break
        out.append({"source": "mind", "term": c["term"], "gloss": c.get("gloss", ""),
                    "category": c.get("category", ""), "variants": c.get("variants", []),
                    "names": c.get("names", {}),          # 外語名 → 跨語通道交叉印證用
                    "is_special": c.get("is_special", False), "score": round(s, 4)})
    return out


async def ground_context(text: str, k: int = settings.mind_retrieve_k, mind_floor: float = settings.local_floor,
                         wiki_per: int = 2, wiki_floor: float = settings.wiki_floor) -> list[dict]:
    """規格 §3.2 接地：chunk → 心智圖實體(top-k,≥mind_floor)〔§3.2①〕→ 每實體橋接其**預存 wiki**
    (≤wiki_per,≥wiki_floor)〔§3.2②〕→ 兩源語境錨點 [mind…, wiki…]〔§3.2③，不改字〕。
    供 `seg.ground_context` 前端顯示完整條目（mind: term/gloss/category/score；wiki: title/aliases/via/score）。
    純讀：用既有 retrieve + card_of 預存 wiki，不動心智圖嵌入。"""
    minds = await retrieve(text, k=k, min_score=mind_floor)        # ① 心智圖檢索（乾淨語境）
    out: list[dict] = list(minds)
    seen: set[str] = set()
    for m in minds:                                               # ② wiki 橋接（每實體預存 wiki）
        c = card_of(m["term"]) or {}
        ws = sorted((w for w in c.get("wiki", []) if w.get("score", 0) >= wiki_floor),
                    key=lambda w: w.get("score", 0), reverse=True)[:wiki_per]
        for w in ws:
            t = (w.get("title") or "").strip()
            if t and t not in seen:
                seen.add(t)
                out.append({"source": "wiki", "via": m["term"], "title": t,
                            "aliases": w.get("aliases", []), "score": round(float(w.get("score", 0)), 4)})
    return out


def mind_links(n: int = 20, floor: float = 0.5, q: str = "") -> dict:
    """wiki 接地頁資料：讀預存 cards 的 wiki 連結組二部圖（q 篩選 term，否則取前 n 個實體）。
    card 增量時 build 已更新預存，這裡只讀、秒回（不實時 embed×N）。floor 供前端再過濾顯示強度。"""
    cards = _cards()
    if q:
        cards = [c for c in cards if q in c.get("term", "")]
    cards = cards[:max(1, min(n, 120))]
    entities = [{"term": c["term"], "gloss": c.get("gloss", ""), "category": c.get("category", ""),
                 "names": c.get("names", {}), "is_special": c.get("is_special", False),
                 "tier": c.get("tier", ""), "source": c.get("source", "")} for c in cards]
    links = {c["term"]: [w for w in c.get("wiki", []) if w.get("score", 0) >= floor] for c in cards}
    return {"entities": entities, "links": links}


def card_of(term: str) -> dict | None:
    """以詞（flatten 後）取心智圖卡（含 vec / 預存 wiki 連結）。供局部語意命中查候選實體向量。"""
    from .variants import flatten
    f = flatten(term)
    for c in _cards():
        if flatten(c.get("term", "")) == f:
            return c
    return None


def top_terms(qvec: list[float], k: int = 5) -> list[tuple[str, float]]:
    """局部句向量 → **最相似的前 k 個心智圖實體** [(term, cos)]（排名制，降冪）。
    **排名制**而非絕對門檻：bge 中文餘弦基線高（任意句對任意實體都 0.35~0.47），唯有「擠進前 k」
    才代表這實體真的是這句在講的（海蝕擠不進「月老婚姻」句的前 k，卻是海蝕句的第一）。"""
    cards = [c for c in _cards() if c.get("vec")]
    scored = sorted(((_cosine(qvec, c["vec"]), c["term"]) for c in cards),
                    key=lambda x: x[0], reverse=True)
    return [(t, round(s, 4)) for s, t in scored[:k]]


def stats() -> dict:
    cards = _cards()
    return {"terms": len(cards), "embedded": sum(1 for c in cards if c.get("vec")),
            "special": sum(1 for c in cards if c.get("is_special")),
            "wiki_linked": sum(1 for c in cards if c.get("wiki"))}


def invalidate() -> None:
    global _cache
    with _lock:
        _cache = None
