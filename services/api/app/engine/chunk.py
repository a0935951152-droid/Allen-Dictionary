"""站1 切分：RAG 式把 raw 逐字稿切成片段 chunk（收斂單位，§3）。

整檔一次跑收斂太慢 → 先切 chunk 再逐塊收斂。預設＝遞迴邊界（段落→句界→標點）
貪婪打包到長度上限 + 相鄰重疊（讓專名即使落在切點仍至少完整出現在某一塊，
收斂訊號不被切分破壞）。語意切分（bge-m3）留為可選升級，見 chunk_semantic()。
"""
from __future__ import annotations

import re

# 在句末標點（含換行/分號）後切，保留標點本身
_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")

# 多語逐字稿（如 SpeakIn 匯出的 zh/en/ja + "--- N" 標記）前處理：只留中文
_DROP_LANG = re.compile(r"^(en|ja|ko|jpn|eng|fr|de|es)\s*[:：]", re.I)
_ZH_PREFIX = re.compile(r"^(zh|cmn|zho|zh-tw|zh-cn|中文)\s*[:：]\s*", re.I)
_MARKER = re.compile(r"^-{2,}\s*\d*\s*$")
_HAN = re.compile(r"[一-鿿]")
_LANG_PREFIX = re.compile(r"^([A-Za-z][\w-]*)\s*[:：]\s*")   # 通用語言前綴 "xxx:"，交 langtag.canon_lang 判 ISO-639-3


_NUM_MARKER = re.compile(r"^-{2,}\s*\d+\s*$")   # 編號單元邊界 "--- 56"


def extract_zh(text: str) -> str:
    """從 zh/en/ja 混排逐字稿抽出中文（去 en:/ja: 行與 --- 標記）。純中文輸入近乎原樣。"""
    out = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if not s or _MARKER.match(s) or _DROP_LANG.match(s):
            continue
        m = _ZH_PREFIX.match(s)
        if m:
            s = s[m.end():].strip()
        elif not _HAN.search(s):
            continue  # 無中文字且無 zh 前綴 → 視為外語/雜訊，丟
        if s:
            out.append(s)
    from .variants import to_tw
    return to_tw("\n".join(out))          # 攝取合規：中文錨統一台灣字面（簡→繁＋大陸用語→台灣用語）


def _has_markers(text: str) -> bool:
    return sum(1 for ln in (text or "").splitlines() if _NUM_MARKER.match(ln.strip())) >= 2


def parse_units(text: str, anchor: str = "cmn") -> list[dict]:
    """解析多語 + "--- N" 逐字稿 → 單元 [{'langs':{iso:text}, 'text':錨語言, 'zh','ref'(相容)}]。
    用 langtag.canon_lang 認**任意**語言前綴（26 語 ISO-639-3），不靠位置（語言順序不固定）。
    **無 ground truth**：所有語言對等存進 langs；錨語言(預設 cmn)當 offset/收斂主體。"""
    from .langtag import canon_lang
    units: list[dict] = []
    cur: dict[str, list] = {}
    started = False
    for ln in (text or "").splitlines():
        s = ln.strip()
        if _NUM_MARKER.match(s):                      # 新編號單元
            if started and cur:
                units.append(cur)
            cur, started = {}, True
            continue
        if _MARKER.match(s):                          # 單元內 "---" 分隔
            continue
        m = _LANG_PREFIX.match(s)
        if m and canon_lang(m.group(1)):              # 任意語言前綴 → ISO-639-3
            cur.setdefault(canon_lang(m.group(1)), []).append(s[m.end():].strip())
        elif _HAN.search(s):                          # 無前綴中文行 → 錨語言
            cur.setdefault(anchor, []).append(s)
    if started and cur:
        units.append(cur)
    from .variants import to_tw
    out = []
    for u in units:
        langs = {iso: " ".join(v).strip() for iso, v in u.items()}
        langs = {k: v for k, v in langs.items() if v}
        if not langs:
            continue
        if langs.get(anchor):
            langs[anchor] = to_tw(langs[anchor])    # 攝取合規：只轉中文錨（外語/日文漢字不碰）
        text_anchor = langs.get(anchor, "")
        ref = "  ".join(v for iso, v in langs.items() if iso != anchor)   # 相容：非錨語言併成 ref
        out.append({"langs": langs, "text": text_anchor, "zh": text_anchor, "ref": ref})
    return out


_HAS_CONTENT = re.compile(r"[一-鿿Ａ-ｚA-Za-z0-9]")   # 至少有一個實字


def _pack(items: list[tuple], max_chars: int) -> list[dict]:
    """便宜貪婪打包（無重疊）：以句界/單元為邊界，累積到 max_chars 就收一塊。
    items＝[(zh, ref)]。回 [{'zh','ref'}]，zh 去標點。"""
    out, cz, cr, n = [], [], [], 0
    for zh, ref in items:
        for piece in (_hard_split(zh, max_chars) if len(zh) > max_chars else [zh]):
            if cz and n + len(piece) > max_chars:
                out.append({"zh": "".join(cz), "ref": " / ".join(x for x in cr if x)})
                cz, cr, n = [], [], 0          # 不重疊（句界切不會切斷詞，重疊只會重複污染）
            cz.append(piece); cr.append(ref); n += len(piece)
    if cz:
        out.append({"zh": "".join(cz), "ref": " / ".join(x for x in cr if x)})
    return out


def make_chunks(raw: str, max_chars: int = 400) -> list[dict]:
    """便宜規則切（對齊論文 RAG 遞迴邊界，無 LLM/embedding）：
    多語逐字稿按 "--- N" 單元邊界、純中文按句界(。！？)貪婪打包，無重疊；
    存下的 zh 去掉所有標點與空白（純實字），剔除空/純標點塊。en/ja 對照存入 ref。"""
    from .variants import strip_punct
    if _has_markers(raw):
        items = [(u["zh"], u["ref"]) for u in parse_units(raw) if u["zh"]]
    else:
        items = [(s, "") for s in split_sentences(extract_zh(raw))]
    cleaned = []
    for c in _pack(items, max_chars):
        zh = strip_punct(c["zh"])              # 去掉所有標點/空白 → 純實字
        if zh and _HAS_CONTENT.search(zh):
            cleaned.append({"zh": zh, "ref": c.get("ref", "")})
    return cleaned


def split_sentences(text: str) -> list[str]:
    parts = [s.strip() for s in _SENT_SPLIT.split(text or "")]
    return [s for s in parts if s]


def _hard_split(s: str, max_chars: int) -> list[str]:
    """單句超過上限時，硬切成定長片段。"""
    return [s[i:i + max_chars] for i in range(0, len(s), max_chars)] or [s]


# ── offset-aware + bge 邊界切塊（手動校正用：core 連續覆蓋、context 重疊只供顯示）──────
# 設計：core 段連續鋪滿全文（不重疊、不留縫）＝覆蓋優先不漏；下游收斂只吃 core（行為不變）。
# context（ctx_before/after）＝相鄰文字的重疊padding，只給「切分站手動校正視圖」看邊界，不往下流。
# bge：在 [lo,hi] 字數窗內把 core 切點對齊「主題轉折」（相鄰單元相似度低谷）；缺服務→規則貪婪降級。
_DEF_OV = 40                              # context 重疊padding 字數（顯示用）


def _cos(a: list[float], b: list[float]) -> float:
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)); nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


async def _sat_split(text: str) -> list[str]:
    """SaT 字節級切句（HTTP，多語、不靠標點）；缺服務/失敗降級規則 split_sentences（天條 3）。"""
    if not (text or "").strip():
        return []
    from .. import clients
    try:
        sents = await clients.sat_split(text)
        if sents:
            return sents
    except Exception:
        pass
    return split_sentences(text)


async def _atoms(raw: str, anchor: str = "cmn") -> tuple[str, list[dict]]:
    """把 raw 攤成 (zh_full, atoms)。atom＝SaT 切出的原子句，帶 langs(多語平行) 與 offset。
    zh_full＝錨語言(cmn)保留標點全文（手動劃分在這條串定位）。多語單元內細切句**繼承單元 langs**。"""
    items: list[tuple] = []
    if _has_markers(raw):
        for u in parse_units(raw, anchor):
            if not u["text"]:
                continue
            for sent in await _sat_split(u["text"]):       # SaT 細切單元內句子
                items.append((sent, u["langs"]))
    else:
        for sent in await _sat_split(extract_zh(raw)):
            items.append((sent, {anchor: sent}))
    zh_full, atoms, pos = "", [], 0
    for text, langs in items:
        for piece in (_hard_split(text, 400) if len(text) > 400 else [text]):
            if not piece:
                continue
            sep = "" if not zh_full else "\n"          # atom 間以 \n 接（offset 連續）
            zh_full += sep + piece
            start = pos + len(sep)
            atoms.append({"text": piece, "langs": langs, "start": start, "end": start + len(piece)})
            pos = start + len(piece)
    return zh_full, atoms


def _pack_valley(atoms: list[dict], lo: int, hi: int, sims: list[float] | None) -> list[tuple[int, int]]:
    """把 atoms 貪婪打包成 core 區段 [(a,b)…)（atom index，連續不重疊）。
    在 [lo,hi] 字數窗內，有 sims 時切在相鄰相似度最低處（主題轉折）；無 sims→盡量靠近 hi（規則）。"""
    n = len(atoms)
    lens = [a["end"] - a["start"] for a in atoms]
    cores, i = [], 0
    while i < n:
        acc, j = 0, i
        while j < n and acc < lo:                      # 先吃到 lo
            acc += lens[j]; j += 1
        # j..延伸到 hi，蒐集候選切點（atom 邊界），挑相似度低谷
        best_e, best_sim, acc2, k = j, 2.0, acc, j
        # 候選＝在 [lo,hi] 內、且 k 在 [i+1, n] 的每個邊界
        e, run = j, acc
        while e <= n and run <= hi:
            if run >= lo:
                s = sims[e - 1] if (sims and e - 1 < len(sims) and e < n) else None
                if s is None:
                    best_e = e                          # 無 bge→取最大（靠近 hi）：續跑
                elif s < best_sim:
                    best_sim, best_e = s, e             # 相似度低谷＝主題轉折，優先切
            if e >= n:
                break
            run += lens[e]; e += 1
        cores.append((i, best_e)); i = best_e
    return cores


async def make_offset_chunks(raw: str, hi: int = 400, lo: int | None = None,
                             ov: int = _DEF_OV) -> list[dict]:
    """切分站主切塊（offset-aware + bge 邊界 + core/context）。回 chunk 清單：
    {start,end, core_text(含標點,顯示/手動劃分), zh(去標點,下游收斂單位),
     ctx_before, ctx_after(重疊padding,只顯示), ref}。core 連續鋪滿＝覆蓋不漏。"""
    from .. import clients
    from .variants import strip_punct
    lo = lo or max(120, hi // 2)
    zh_full, atoms = await _atoms(raw)
    if not atoms:
        return []
    sims: list[float] | None = None
    try:                                               # bge：相鄰 atom 相似度（低谷＝主題轉折切點）
        vecs = await clients.embed([a["text"] for a in atoms])
        sims = [_cos(vecs[i], vecs[i + 1]) for i in range(len(vecs) - 1)]
    except Exception:
        sims = None                                    # 封閉降級：無 bge→規則貪婪
    out = []
    n = len(atoms)
    for a, b in _pack_valley(atoms, lo, hi, sims):
        # core 連續鋪滿：end 延伸到下一塊起點（吃掉 atom 間 \n 分隔），最後一塊到全文尾 → 無縫覆蓋
        start = atoms[a]["start"]
        end = atoms[b]["start"] if b < n else len(zh_full)
        core_text = zh_full[start:end]
        langs: dict[str, list] = {}                    # 合併區段內各 atom 的多語平行版本
        for k in range(a, b):
            for iso, t in atoms[k].get("langs", {}).items():
                if t:
                    langs.setdefault(iso, []).append(t)
        langs_merged = {iso: " ".join(v) for iso, v in langs.items()}
        ref = " / ".join(v for iso, v in langs_merged.items() if iso != "cmn")   # 相容：非錨語言併成 ref
        out.append({"start": start, "end": end, "core_text": core_text,
                    "zh": strip_punct(core_text), "langs": langs_merged, "ref": ref,
                    "ctx_before": zh_full[max(0, start - ov):start],
                    "ctx_after": zh_full[end:end + ov]})
    return out


def chunk_text(text: str, max_chars: int = 400, overlap_sentences: int = 1) -> list[str]:
    """RAG 式：遞迴邊界貪婪打包 + 相鄰重疊。回傳 chunk 字串清單。"""
    sents = split_sentences(text)
    if not sents:
        return []
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for s in sents:
        for p in (_hard_split(s, max_chars) if len(s) > max_chars else [s]):
            if cur and cur_len + len(p) > max_chars:
                chunks.append("".join(cur))
                # 重疊：把末 overlap_sentences 句帶入下一塊
                cur = cur[-overlap_sentences:] if overlap_sentences > 0 else []
                cur_len = sum(len(x) for x in cur)
            cur.append(p)
            cur_len += len(p)
    if cur:
        chunks.append("".join(cur))
    return chunks
