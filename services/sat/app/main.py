"""SaT 句分割封閉服務（§3 切分前置）：segment-any-text/sat-3l-sm，CPU、字節級、多語、不靠標點。

建構期(host)裝 torch CPU + wtpsplit；runtime 全離線（HF_HUB_OFFLINE=1），模型從 /models 本地載入。
api 的 chunk 切分以 HTTP 呼叫此服務取代規則 split_sentences；本服務不可用時 api 自動降級規則切（天條 3）。
"""
import os

from fastapi import FastAPI
from pydantic import BaseModel

MODEL = os.environ.get("SAT_MODEL_PATH", "/models/sat/sat-3l-sm")
TOK = os.environ.get("SAT_TOKENIZER_PATH", "/models/sat/xlm-roberta-base")   # XLM-R tokenizer（離線本地）
app = FastAPI()
_sat = None


def _model():
    global _sat
    if _sat is None:
        from wtpsplit import SaT
        _sat = SaT(MODEL, tokenizer_name_or_path=TOK)   # 模型 + tokenizer 皆本地離線載入
    return _sat


class SplitIn(BaseModel):
    text: str = ""
    threshold: float | None = None             # 句界機率門檻（越高切越少；省略用模型預設）


@app.get("/health")
def health():
    try:
        _model()
        return {"ok": True, "model": os.path.basename(MODEL)}
    except Exception as e:                      # 載入失敗也回 200，讓 api 端走降級而非整批失敗
        return {"ok": False, "error": str(e)}


@app.post("/split")
def split(body: SplitIn):
    """文本 → 句子清單（字節級多語切句，不靠標點）。空輸入回空。"""
    if not (body.text or "").strip():
        return {"sentences": []}
    sat = _model()
    kw = {"threshold": body.threshold} if body.threshold is not None else {}
    sents = sat.split(body.text, **kw)
    return {"sentences": [s.strip() for s in sents if s and s.strip()]}
