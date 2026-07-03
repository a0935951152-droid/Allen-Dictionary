"""知識側自動萃取（聲學檢索的參考詞表產生器，§3/§9）。

目標：**不手寫詞表**。把乾淨來源（簡報/PDF/講義、既有詞表、本地語料）自動萃成標準 JSON
丟進 `data/files/refterms/`，餵 phonetic_index。全地端、無上網。

鐵律：知識側只能從**比待校稿更乾淨**的來源抽（材料/詞表/已校正稿），不可從錯誤 ASR 稿自抽
（否則 `豆腐沿` 會被當詞索引、污染正解）。

三條萃取路（可組合）：
1. 詞表模式（harvest_glossary）：來源本身就是詞表 → 正規化，信任度高。
2. 新詞發現（discover_words）：純統計、無 LLM、無標註——凝固度(PMI)＋左右熵，從生文本撈
   字典沒有的領域多字詞（豆腐岩、海蝕平台）。中文新詞發現經典法。
3. 本地 NER（clients.ner，ckip）：抽專名補強。
（可選）本地 LLM 精煉：丟現有 vLLM judge 列術語，預設關（保持確定性/省算力）。
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

from .. import clients

_HAN_RUN = re.compile(r"[一-鿿]+")        # 連續中文段（不跨非中文邊界，避免假詞）
_SKIP_NER = {"CARDINAL", "DATE", "MONEY", "ORDINAL", "PERCENT", "QUANTITY", "TIME"}

# 通用高頻詞停用（避免把常用詞當領域術語）；可再擴充
_STOP = set("我們你們他們這個那個這些那些什麼怎麼可以因為所以但是而且然後就是這樣那樣沒有不是"
            "如果現在已經還有大家今天時候問題方面進行表示認為知道覺得開始繼續")


# ── 新詞發現（凝固度 + 左右熵，純地端統計）────────────────
def discover_words(text: str, max_len: int = 6, min_freq: int = 3,
                   min_cohesion: float = 1.8, min_entropy: float = 0.8,
                   top: int = 800) -> list[dict]:
    """從生文本撈領域新詞。回 [{term,source,confidence,freq}]（無監督、無 LLM）。

    凝固度＝log( P(w) / max_split P(left)·P(right) )：字黏得越緊越高（互資訊）。
    左右熵＝min(H(左鄰), H(右鄰))：左右搭配越自由＝越像獨立的詞（非更大固定片段的碎片）。
    """
    segs = _HAN_RUN.findall(text or "")
    total = sum(len(s) for s in segs)
    if total == 0:
        return []

    cnt: dict[str, int] = defaultdict(int)
    left: dict[str, Counter] = defaultdict(Counter)
    right: dict[str, Counter] = defaultdict(Counter)
    for s in segs:
        L = len(s)
        for i in range(L):
            for n in range(1, max_len + 1):
                if i + n > L:
                    break
                w = s[i:i + n]
                cnt[w] += 1
                left[w][s[i - 1] if i - 1 >= 0 else "^"] += 1
                right[w][s[i + n] if i + n < L else "$"] += 1

    def P(w: str) -> float:
        return cnt[w] / total

    def H(counter: Counter) -> float:
        tot = sum(counter.values())
        if not tot:
            return 0.0
        return -sum((c / tot) * math.log(c / tot) for c in counter.values())

    out: list[dict] = []
    for w, c in cnt.items():
        if len(w) < 2 or c < min_freq or w in _STOP:
            continue
        best_joint = max(P(w[:i]) * P(w[i:]) for i in range(1, len(w)))
        if best_joint <= 0:
            continue
        cohesion = math.log(P(w) / best_joint)
        if cohesion < min_cohesion:
            continue
        free = min(H(left[w]), H(right[w]))
        if free < min_entropy:
            continue
        score = cohesion + free
        # 信心壓到 ~[0,1]（粗略；使用者不缺準度，主要供排序/審計）
        conf = round(min(1.0, 0.35 + 0.08 * cohesion + 0.12 * free), 3)
        out.append({"term": w, "source": "newword", "confidence": conf,
                    "freq": c, "_score": round(score, 3)})

    out.sort(key=lambda x: -x["_score"])
    # 碎片抑制：較短候選若幾乎只作為某更高分長詞的一部分出現 → 丟（左右熵多半已擋，這裡補刀）
    kept: list[dict] = []
    longer = []
    for e in out:
        w = e["term"]
        if any(w in lw and e["freq"] <= int(cnt[lw] * 1.2) for lw in longer):
            continue
        kept.append(e)
        longer.append(w)
    for e in kept:
        e.pop("_score", None)
    return kept[:top]


# ── 詞表模式：來源已是詞表 → 正規化 ──────────────────────
def harvest_glossary(text: str, confidence: float = 0.95) -> list[dict]:
    """一行一詞（# 註解略）→ 標準詞條（高信任）。"""
    seen, out = set(), []
    for ln in (text or "").splitlines():
        w = ln.strip()
        if not w or w.startswith("#") or w in seen:
            continue
        seen.add(w)
        out.append({"term": w, "source": "glossary", "confidence": confidence})
    return out


# ── NER 路：本地 ckip 抽專名 ─────────────────────────────
async def harvest_ner(text: str, confidence: float = 0.8) -> list[dict]:
    try:
        ents = await clients.ner(text)
    except Exception:
        return []
    seen, out = set(), []
    for e in ents:
        w = (e.get("word") or "").strip()
        if (not w or len(w) < 2 or e.get("type") in _SKIP_NER or w in seen or w in _STOP):
            continue
        seen.add(w)
        out.append({"term": w, "source": "ner", "confidence": confidence})
    return out


# ── 文件模式：自由文本 → NER + 新詞發現（＋可選 LLM）──────
async def harvest_text(text: str, use_ner: bool = True, use_newword: bool = True,
                       use_llm: bool = False) -> list[dict]:
    """乾淨材料自由文本 → 合併詞條（去重，較高信心優先）。"""
    bag: list[dict] = []
    if use_newword:
        bag += discover_words(text)
    if use_ner:
        bag += await harvest_ner(text)
    if use_llm:
        bag += await _harvest_llm(text)
    # 去重：同 term 取最高信心
    best: dict[str, dict] = {}
    for e in bag:
        t = e["term"]
        if t not in best or e["confidence"] > best[t]["confidence"]:
            best[t] = {k: v for k, v in e.items() if k != "freq"}
    return sorted(best.values(), key=lambda x: -x["confidence"])


async def _harvest_llm(text: str, confidence: float = 0.85) -> list[dict]:
    """可選：本地 vLLM judge 列術語/專名（地端）。失敗回空。"""
    prompt = ("你是術語抽取器。從下列領域文本列出『專有名詞與領域術語』（人名/地名/機構/"
              "技術詞/專名），每個都要是文本中出現的詞，不要解釋。只輸出 JSON 字串陣列。\n\n文本：\n"
              + text[:4000])
    try:
        import json
        raw = await clients.judge_chat(prompt, max_tokens=512)
        m = re.search(r"\[.*\]", raw, re.S)
        arr = json.loads(m.group(0)) if m else []
    except Exception:
        return []
    seen, out = set(), []
    for w in arr:
        w = str(w).strip()
        if w and len(w) >= 2 and w not in seen and w in text:
            seen.add(w)
            out.append({"term": w, "source": "llm", "confidence": confidence})
    return out
