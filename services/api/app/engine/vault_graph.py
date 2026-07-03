"""Obsidian vault → 心智圖圖譜 JSON（後端，§4 可視化）。

讀 `knowledge/vault/terms/*.md` 的 frontmatter + [[links]]，組成 {nodes, edges} 給前端
force-directed 心智圖瀏覽。不依賴 Obsidian——純讀 markdown，自帶 web graph viewer。
"""
from __future__ import annotations

import os
import re

from ..config import settings

_TERMS = os.path.join(settings.data_dir, "knowledge", "vault", "terms")
_LINK = re.compile(r"\[\[([^\]]+)\]\]")


def _parse_note(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            txt = f.read()
    except Exception:
        return None
    meta: dict = {"term": "", "term_lang": "", "category": "", "source": "", "confidence": 0.0,
                  "ipa": "", "tier": "", "reason": "", "is_special": False,
                  "gloss": "", "names": {}, "names_src": {}, "variants": [], "links": []}
    if txt.startswith("---"):
        end = txt.find("\n---", 3)
        fm, body = txt[3:end], txt[end + 4:]
    else:
        fm, body = "", txt
    for ln in fm.splitlines():
        if ":" not in ln:
            continue
        k, v = ln.split(":", 1)
        k, v = k.strip(), v.strip()
        if k == "confidence":
            try:
                meta[k] = float(v)
            except ValueError:
                pass
        elif k == "variants":
            meta[k] = [x.strip() for x in v.strip("[]").split(",") if x.strip()]
        elif k in ("names", "names_src"):        # {eng: x, jpn: y} → dict
            meta[k] = {p.split(":", 1)[0].strip(): p.split(":", 1)[1].strip()
                       for p in v.strip("{}").split(",")
                       if ":" in p and p.split(":", 1)[1].strip()}
        elif k == "is_special":
            meta[k] = v.lower() == "true"
        elif k == "pinyin_key":                  # 舊 md 相容：拼音欄位 → ipa（下次 build 即遷移）
            meta["ipa"] = meta["ipa"] or v
        elif k in meta:
            meta[k] = v
    g = re.search(r"^\*(.+)\*$", body, re.M)     # body 的 *gloss* 斜體行
    if g:
        meta["gloss"] = g.group(1).strip()
    meta["links"] = list(dict.fromkeys(_LINK.findall(body)))   # 去重保序
    if not meta["term"]:
        meta["term"] = os.path.splitext(os.path.basename(path))[0]
    return meta


def graph(category: str | None = None, source: str | None = None,
          min_conf: float = 0.0, q: str | None = None, limit: int = 400) -> dict:
    """組圖（含過濾）。source 比對前綴（glossary/newword/llm）。回 {nodes, edges, stats}。"""
    if not os.path.isdir(_TERMS):
        return {"nodes": [], "edges": [], "stats": {"terms": 0, "shown": 0}}
    src_colors = {"glossary": "#2d8cf0", "newword": "#9aa0a6", "llm": "#39b54a",
                  "ner": "#f0962d", "dict": "#7b61ff"}
    notes, total = [], 0
    for fn in sorted(os.listdir(_TERMS)):
        if not fn.endswith(".md"):
            continue
        m = _parse_note(os.path.join(_TERMS, fn))
        if not m:
            continue
        total += 1                                  # 永遠累加 → stats.terms 反映真實總數（修 bug #3：原 break 後漏計）
        if category and category not in m["category"]:
            continue
        if source and not m["source"].startswith(source):
            continue
        if m["confidence"] < min_conf:
            continue
        if q and q not in m["term"]:
            continue
        if len(notes) < limit:                      # 達上限只停止收集、仍續數 total
            notes.append(m)

    term_ids = {m["term"] for m in notes}
    nodes, edges, dom_seen = [], [], set()
    for m in notes:
        src_kind = (m["source"].split(":", 1)[0]) or "?"
        nodes.append({"id": m["term"], "kind": "term", "category": m["category"],
                      "source": src_kind, "confidence": m["confidence"],
                      "color": src_colors.get(src_kind, "#9aa0a6"),
                      "variants": m["variants"], "ipa": m["ipa"],
                      "term_lang": m["term_lang"], "tier": m["tier"], "reason": m["reason"],
                      "is_special": m["is_special"], "gloss": m["gloss"],
                      "names": m["names"], "names_src": m["names_src"]})
        for tgt in m["links"]:
            if tgt.startswith("domains/"):
                dom = tgt.split("/", 1)[1]
                if dom not in dom_seen:
                    dom_seen.add(dom)
                    nodes.append({"id": tgt, "kind": "domain", "label": dom,
                                  "color": "#111", "category": dom})
                edges.append({"from": m["term"], "to": tgt, "type": "domain"})
            elif tgt in term_ids:
                edges.append({"from": m["term"], "to": tgt, "type": "rel"})
            # 連到變體/未建 note 的目標：略過（避免大量孤點），變體已存 node.variants
    return {"nodes": nodes, "edges": edges,
            "stats": {"terms": total, "shown": len(notes), "domains": len(dom_seen)}}


def categories() -> list[dict]:
    """各 category 的詞數（供前端過濾下拉）。"""
    from collections import Counter
    c: Counter = Counter()
    if os.path.isdir(_TERMS):
        for fn in os.listdir(_TERMS):
            if fn.endswith(".md"):
                m = _parse_note(os.path.join(_TERMS, fn))
                if m and m["category"]:
                    c[m["category"]] += 1
    return [{"category": k, "count": v} for k, v in c.most_common()]
