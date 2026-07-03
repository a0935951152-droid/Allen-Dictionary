"""wiki 大庫向量索引（§4.1 接地廣度語料）：bge-m3 嵌入 + hnswlib 近鄰，支援增量。

- 來源：`wiki_ingest.build()` 產的 `data/wiki/entries.jsonl`（{title,aliases,pageid}）。
- 嵌入：每條文字 `title｜aliases`（摘要待補）→ `clients.embed`（bge-m3, TEI）分批。
- 索引：hnswlib cosine 圖，落地 `data/index/wiki/hnsw.bin` + `ids.jsonl`（label→卡）+ `meta.json`。
- **增量**（心智圖/語料會長大）：`add_entries()` 對新條目 embed + `add_items`（resize 擴容）；
  `build()` 全量重建（src dump 換新時）。dim 由首批嵌入自動偵測。

降級：缺 hnswlib / embed 服務 → search 回空（封閉環境不壞，比照 ground.py best-effort）。
治理鐵律：wiki 一律 candidate，只當接地語境/候選，永不自動升 gold。
"""
from __future__ import annotations

import json
import os
import threading

from .. import clients

try:
    import hnswlib
    _HNSW_OK = True
except Exception:
    _HNSW_OK = False

_DATA = os.environ.get("DATA_DIR", "/data")
_ENTRIES = os.path.join(_DATA, "wiki", "entries.jsonl")
_DIR = os.path.join(_DATA, "index", "wiki")
_BIN = os.path.join(_DIR, "hnsw.bin")
_IDS = os.path.join(_DIR, "ids.jsonl")
_META = os.path.join(_DIR, "meta.json")

_EF_CONSTRUCTION = 200
_M = 16
_EF_QUERY = 64
_BATCH = 128                       # 每次送 TEI 的條數（短標題，吞吐高）

_lock = threading.Lock()
_state: dict | None = None          # {"index", "cards":[card], "dim", "n"}


def _card_text(rec: dict) -> str:
    """條目 → 待嵌入文字：標題 + 別名（摘要版之後接上）。"""
    al = rec.get("aliases") or []
    return rec["title"] + ("｜" + "｜".join(al) if al else "")


def _count_lines(path: str) -> int:
    n = 0
    with open(path, "rb") as f:
        for _ in f:
            n += 1
    return n


# ── 全量建置（一次性；長跑，建議 python -m app.engine.wiki_index）──────────
async def build(batch: int = _BATCH) -> dict:
    """讀 entries.jsonl → 分批嵌入 → hnswlib 全量建索引並落地。回統計。"""
    if not _HNSW_OK:
        raise RuntimeError("hnswlib 未安裝")
    if not os.path.exists(_ENTRIES):
        raise FileNotFoundError(f"缺 {_ENTRIES}（先跑 wiki_ingest）")
    total = _count_lines(_ENTRIES)
    os.makedirs(_DIR, exist_ok=True)

    index = None
    dim = 0
    done = 0
    buf_text: list[str] = []
    buf_card: list[dict] = []
    ids_tmp = _IDS + ".tmp"
    fids = open(ids_tmp, "w", encoding="utf-8")

    async def flush():
        nonlocal index, dim, done
        if not buf_text:
            return
        vecs = await clients.embed(buf_text)
        if index is None:                      # 首批 → 定 dim、init
            dim = len(vecs[0])
            index = hnswlib.Index(space="cosine", dim=dim)
            index.init_index(max_elements=total, ef_construction=_EF_CONSTRUCTION, M=_M)
        labels = list(range(done, done + len(vecs)))
        index.add_items(vecs, labels)
        for card in buf_card:
            fids.write(json.dumps(card, ensure_ascii=False) + "\n")
        done += len(vecs)
        buf_text.clear(); buf_card.clear()

    try:
        with open(_ENTRIES, encoding="utf-8") as f:
            for line in f:
                rec = json.loads(line)
                buf_text.append(_card_text(rec))
                buf_card.append({"title": rec["title"], "aliases": rec.get("aliases", [])})
                if len(buf_text) >= batch:
                    await flush()
            await flush()
    finally:
        fids.close()

    if index is None:
        raise RuntimeError("無條目可建索引")
    index.save_index(_BIN)
    os.replace(ids_tmp, _IDS)
    meta = {"n": done, "dim": dim, "space": "cosine", "max_elements": total,
            "model": "bge-m3", "fields": "title|aliases"}
    with open(_META, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    with _lock:                                # 失效記憶體快取，下次 search 重載
        global _state
        _state = None
    return meta


# ── 增量新增（心智圖/語料長大；只 embed 新條目）─────────────────────────
async def add_entries(records: list[dict], batch: int = _BATCH) -> dict:
    """對既有索引 add_items 新條目（resize 擴容）。records: [{title,aliases}]。回統計。"""
    if not _HNSW_OK or not os.path.exists(_BIN):
        raise RuntimeError("索引未建，先 build()")
    st = _load()
    index, cards, dim = st["index"], st["cards"], st["dim"]
    have = {c["title"] for c in cards}
    new = [r for r in records if r["title"] not in have]
    if not new:
        return {"added": 0, "n": st["n"]}
    index.resize_index(st["n"] + len(new))
    added = 0
    with open(_IDS, "a", encoding="utf-8") as fids:
        for i in range(0, len(new), batch):
            chunk = new[i:i + batch]
            vecs = await clients.embed([_card_text(r) for r in chunk])
            labels = list(range(st["n"] + added, st["n"] + added + len(vecs)))
            index.add_items(vecs, labels)
            for r in chunk:
                card = {"title": r["title"], "aliases": r.get("aliases", [])}
                cards.append(card)
                fids.write(json.dumps(card, ensure_ascii=False) + "\n")
            added += len(vecs)
    index.save_index(_BIN)
    st["n"] += added
    with open(_META, encoding="utf-8") as f:
        meta = json.load(f)
    meta["n"] = st["n"]
    with open(_META, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    return {"added": added, "n": st["n"]}


# ── 載入 / 查詢 ───────────────────────────────────────────────
def _load() -> dict:
    global _state
    with _lock:
        if _state is not None:
            return _state
        if not _HNSW_OK or not os.path.exists(_BIN):
            raise RuntimeError("wiki 索引未就緒")
        with open(_META, encoding="utf-8") as f:
            meta = json.load(f)
        index = hnswlib.Index(space=meta["space"], dim=meta["dim"])
        index.load_index(_BIN, max_elements=max(meta["max_elements"], meta["n"]))
        index.set_ef(_EF_QUERY)
        cards = [json.loads(l) for l in open(_IDS, encoding="utf-8")]
        _state = {"index": index, "cards": cards, "dim": meta["dim"], "n": meta["n"]}
        return _state


def search_vec(vec, k: int = 8) -> list[dict]:
    """用**現成向量**檢索 wiki（心智圖 card 已嵌入的 vec 直接查，免重嵌；同步、純 hnsw）。未就緒回空。"""
    if vec is None:
        return []
    try:
        st = _load()
        labels, dists = st["index"].knn_query(vec, k=min(k, st["n"]))
        return [{"title": st["cards"][int(lab)]["title"],
                 "aliases": st["cards"][int(lab)].get("aliases", []),
                 "score": round(1.0 - float(d), 4)}                # cosine 距離→相似度
                for lab, d in zip(labels[0], dists[0])]
    except Exception:
        return []


async def search(text: str, k: int = 8) -> list[dict]:
    """語意檢索 wiki 大庫 → 回 [{title,aliases,score}]（best-effort，未就緒回空）。先 embed 文字再查。"""
    if not text:
        return []
    try:
        qv = (await clients.embed([text]))[0]
    except Exception:
        return []
    return search_vec(qv, k=k)


def ready() -> bool:
    return _HNSW_OK and os.path.exists(_BIN)


def stats() -> dict:
    if not os.path.exists(_META):
        return {"ready": False, "hnswlib": _HNSW_OK}
    with open(_META, encoding="utf-8") as f:
        meta = json.load(f)
    return {"ready": ready(), "hnswlib": _HNSW_OK, **meta}


def invalidate() -> None:
    global _state
    with _lock:
        _state = None


if __name__ == "__main__":
    import asyncio
    import sys
    print(json.dumps(asyncio.run(build()), ensure_ascii=False, indent=1), file=sys.stderr)
