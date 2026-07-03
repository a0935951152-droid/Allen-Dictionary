"""NER 封閉服務（§3 篩選節點「本地 Chinese NER」）。

ckiplab/bert-base-chinese-ner 抽出命名實體當「候選專名」——便宜濾網，篩掉約 9 成常用詞，
只有實體才往下進收斂/接地。CPU 推論、模型本地載入、runtime 離線。
"""
from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

MODEL = os.environ.get("NER_MODEL_PATH", "/models/ner/ckip-bert-base-chinese-ner")

app = FastAPI(title="NER 服務", version="0.1.0")
_pipe = None


def pipe():
    global _pipe
    if _pipe is None:
        from transformers import (AutoModelForTokenClassification,
                                  AutoTokenizer, pipeline)
        tok = AutoTokenizer.from_pretrained(MODEL, use_fast=True)
        mdl = AutoModelForTokenClassification.from_pretrained(MODEL)
        # aggregation_strategy="none"：拿每字原始 BIOES 標籤，自己精解析（HF simple 不懂 E-/S-，會碎片化）
        _pipe = pipeline("token-classification", model=mdl, tokenizer=tok,
                         aggregation_strategy="none", device=-1)
    return _pipe


def _parse_bioes(tokens: list, text: str) -> list:
    """最細緻讀：逐字 BIOES 標籤 → 完整實體 span。
    B/E 切邊界 → 相鄰同類實體正確分開（台積電|宏碁，非黏成一個）。相容純 BIO（B 亦閉合前一實體）。"""
    out: list = []
    cur: dict | None = None
    for tk in tokens:
        lab = tk.get("entity") or tk.get("entity_group") or "O"
        s, e = int(tk["start"]), int(tk["end"])
        sc = float(tk.get("score", 1.0))
        if lab == "O" or "-" not in lab:               # 非實體 → 閉合
            if cur:
                out.append(cur); cur = None
            continue
        pos, typ = lab.split("-", 1)                    # B/I/E/S, 類型
        if pos == "B":                                  # 新實體開頭：先閉合前一個（切開相鄰同類）
            if cur:
                out.append(cur)
            cur = {"start": s, "end": e, "type": typ, "score": sc}
        elif pos == "S":                                # 單字實體
            if cur:
                out.append(cur); cur = None
            out.append({"start": s, "end": e, "type": typ, "score": sc})
        else:                                           # I / E：延續同類
            if cur and cur["type"] == typ:
                cur["end"] = e; cur["score"] = min(cur["score"], sc)
            else:                                       # 容錯：I/E 無對應 B → 當新實體起點
                if cur:
                    out.append(cur)
                cur = {"start": s, "end": e, "type": typ, "score": sc}
            if pos == "E":                              # E 結尾 → 閉合
                out.append(cur); cur = None
    if cur:
        out.append(cur)
    for o in out:                                       # 補 surface
        o["word"] = text[o["start"]:o["end"]].strip()
        o["score"] = round(o["score"], 4)
    return [o for o in out if o["word"]]


@app.get("/health")
def health():
    try:
        pipe()
        return {"ok": True}
    except Exception as e:  # 模型載入失敗 → 503，讓 healthcheck 反映實況
        raise HTTPException(503, str(e)[:200])


class NerIn(BaseModel):
    text: str


@app.post("/ner")
def ner(body: NerIn):
    text = body.text or ""
    if not text:
        return {"entities": []}
    return {"entities": _parse_bioes(pipe()(text), text)}    # BIOES 精解析 → 完整實體（相鄰正確切分）
