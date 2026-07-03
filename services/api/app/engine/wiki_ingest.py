"""wiki 大庫攝取（離線、純 CPU、一次性）：zhwiki dump → entries.jsonl（標題+別名）。

接地大庫（§4.1 第二次 hybrid 的廣度語料）的**標題+redirect 先行版**（摘要待後續增量補）。
- 來源（data/wiki/raw/）：
  - `*-page.sql.gz`     ：page 表（page_id, namespace, title, is_redirect）→ 正規條目（ns0、非 redirect）。
  - `*-redirect.sql.gz` ：redirect 表（rd_from→rd_title）→ 別名（ASR 易混的正名異寫，entity-linking 金礦）。
- 正規化：底線→空白、OpenCC s2twp 轉繁（與語料 zh-Hant 一致）。
- 輸出 `data/wiki/entries.jsonl`：每行 {"title","aliases":[...],"pageid"}；
  `data/wiki/manifest.json`：dump 來源 + 條目數 + 每條 hash（供 wiki_index 增量）。

治理鐵律：wiki 一律 candidate，永不自動升 gold（只當接地語境/候選，不直接改字）。
MySQL dump 逐 tuple 串流解析（page.sql 解壓 >1GB，不整檔載入）。
"""
from __future__ import annotations

import glob
import gzip
import hashlib
import json
import os
import re

try:
    from opencc import OpenCC
    _CC = OpenCC("s2twp")
except Exception:                       # 缺 opencc → 不轉換（降級不壞）
    _CC = None

_WIKI_DIR = os.path.join(os.environ.get("DATA_DIR", "/data"), "wiki")
_RAW = os.path.join(_WIKI_DIR, "raw")
_ENTRIES = os.path.join(_WIKI_DIR, "entries.jsonl")
_MANIFEST = os.path.join(_WIKI_DIR, "manifest.json")


def _to_twp(s: str) -> str:
    s = s.replace("_", " ").strip()
    if _CC and s:
        try:
            return _CC.convert(s)
        except Exception:
            return s
    return s


def _find(pattern: str) -> str | None:
    hits = sorted(glob.glob(os.path.join(_RAW, pattern)))
    return hits[-1] if hits else None


# ── MySQL dump 逐列解析 ───────────────────────────────────────
# INSERT INTO `t` VALUES (a,b,'c'),(d,...);  → 逐 tuple yield 欄位字串 list。
# 單引號字串內 \' \\ \" 轉義；數字/NULL 原樣。串流逐行、不整檔載入。
_VALUES = re.compile(rb"VALUES\s*(.+);\s*$", re.S)   # group=（t1),(t2),...,(tN）含全部括號


def _iter_sql_rows(path: str):
    """yield 每列的欄位 list（字串已去引號解轉義；數字/NULL 為原始 str）。"""
    op = gzip.open(path, "rb")
    try:
        for line in op:
            if not line.startswith(b"INSERT INTO"):
                continue
            m = _VALUES.search(line)
            if not m:
                continue
            yield from _split_tuples(m.group(1))
    finally:
        op.close()


def _split_tuples(blob: bytes):
    """把 `(..),(..),..` 切成每 tuple 的欄位 list。手寫狀態機處理引號/轉義；
    tuple 內非結構字元（數字/小數/NULL）逐一收進欄位。"""
    field = bytearray()
    row: list = []
    in_str = False
    esc = False
    depth = 0
    for c in blob:
        if in_str:
            if esc:
                field.append(c); esc = False
            elif c == 0x5C:                 # backslash 轉義
                esc = True
            elif c == 0x27:                 # closing '
                in_str = False
            else:
                field.append(c)
        elif c == 0x27:                     # opening '
            in_str = True
        elif c == 0x28:                     # (
            if depth == 0:
                field = bytearray(); row = []
            depth += 1
        elif c == 0x29:                     # )
            depth -= 1
            if depth == 0:
                row.append(field)
                yield [bytes(f).decode("utf-8", "replace") for f in row]
        elif c == 0x2C and depth == 1:      # , 欄位分隔（tuple 內）
            row.append(field); field = bytearray()
        elif depth == 1:                    # tuple 內非結構字元（數字/NULL）→ 收進欄位
            field.append(c)


def _detect_redirect_idx(page_path: str) -> int:
    """偵測 page tuple 中 is_redirect 欄位 index（modern schema=3；舊含 restrictions=4）。
    取樣前幾列：欄位[3] 是 '0'/'1' → 3，否則 4。"""
    for r in _iter_sql_rows(page_path):
        if len(r) > 4 and r[1] == "0":
            return 3 if r[3] in ("0", "1") else 4
    return 3


def build(min_title_len: int = 1, max_aliases: int = 12) -> dict:
    """解析 page+redirect → entries.jsonl + manifest。回統計。"""
    page_path = _find("*-page.sql.gz")
    redir_path = _find("*-redirect.sql.gz")
    if not page_path or not redir_path:
        raise FileNotFoundError(f"缺 page/redirect dump 於 {_RAW}")

    ri = _detect_redirect_idx(page_path)
    id2title: dict[int, str] = {}          # ns0 page_id → 原始 title（解 redirect 別名用）
    canon: dict[str, int] = {}             # 正規 title(twp) → pageid（ns0、非 redirect）
    raw2twp: dict[str, str] = {}           # 原始 title → twp（別名映射用，省重複轉換）

    for r in _iter_sql_rows(page_path):    # page：(id, ns, title, [restrictions,] is_redirect, …)
        if len(r) <= ri or r[1] != "0":
            continue
        try:
            pid = int(r[0])
        except ValueError:
            continue
        raw_title = r[2]
        id2title[pid] = raw_title
        if r[ri] == "0":                   # 非 redirect → 正規條目
            t = _to_twp(raw_title)
            if len(t) >= min_title_len:
                canon.setdefault(t, pid)
                raw2twp[raw_title] = t

    aliases: dict[str, list[str]] = {}     # 正規 title → [別名 twp]
    for r in _iter_sql_rows(redir_path):   # redirect：(rd_from, rd_namespace, rd_title, …)
        if len(r) < 3 or r[1] != "0":
            continue
        try:
            frm = int(r[0])
        except ValueError:
            continue
        src_raw = id2title.get(frm)        # 別名頁的標題
        if not src_raw:
            continue
        tgt = _to_twp(r[2])                # 指向的正規 title
        if tgt not in canon:
            continue
        alias = _to_twp(src_raw)
        if alias and alias != tgt:
            lst = aliases.setdefault(tgt, [])
            if alias not in lst and len(lst) < max_aliases:
                lst.append(alias)

    os.makedirs(_WIKI_DIR, exist_ok=True)
    tmp = _ENTRIES + ".tmp"
    n = 0
    with open(tmp, "w", encoding="utf-8") as f:
        for title, pid in canon.items():
            rec = {"title": title, "aliases": aliases.get(title, []), "pageid": pid}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    os.replace(tmp, _ENTRIES)

    stat = {"entries": n, "aliases_total": sum(len(v) for v in aliases.values()),
            "redirect_idx": ri, "page_dump": os.path.basename(page_path),
            "redirect_dump": os.path.basename(redir_path),
            "src_hash": _src_hash(page_path, redir_path)}
    with open(_MANIFEST, "w", encoding="utf-8") as f:
        json.dump(stat, f, ensure_ascii=False, indent=1)
    return stat


def _src_hash(*paths: str) -> str:
    h = hashlib.sha256()
    for p in paths:
        st = os.stat(p)
        h.update(f"{os.path.basename(p)}:{st.st_size}:{int(st.st_mtime)}".encode())
    return h.hexdigest()[:16]


if __name__ == "__main__":
    import sys
    print(json.dumps(build(), ensure_ascii=False, indent=1), file=sys.stderr)
