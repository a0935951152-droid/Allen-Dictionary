"""節點資料模型（對應架構 §5 的 JSON schema）。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Decision = Literal["pending", "auto", "human", "regex"]
Author = Literal["model", "human"]
Review = Literal["正確", "存疑", "錯字"]


# ── §5 SpanNode ────────────────────────────────────────────────
class HistoryItem(BaseModel):
    iter: int
    value: str
    by: Author = "model"


class Grounding(BaseModel):
    checked: bool = False
    pass_: bool = Field(False, alias="pass")
    url: Optional[str] = None
    rrf_score: float = 0.0
    candidates: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class DecisionInfo(BaseModel):
    to: Decision = "pending"
    correct: Optional[str] = None
    regex: Optional[str] = None


class ReportItem(BaseModel):
    """一份證據報告裡的一個候選（音/義/跨語各自產出）。"""
    candidate: str = ""                # 正解候選
    score: float = 0.0                 # 該通道支持分數（已正規化，越高越支持）
    evidence: str = ""                 # 證據簡述（IPA 距離 / 接地來源+分數 / 對齊語言）


class SpanReports(BaseModel):
    """三份證據報告 → gemma 彙整仲裁（不信自報 conf，看三路一致性）。§4.1 精修輸入。"""
    acoustic: list[ReportItem] = Field(default_factory=list)      # 聲學：IPA→CAG 同音正解候選
    semantic: list[ReportItem] = Field(default_factory=list)      # 語意：心智圖/wiki 接地支持的候選
    crosslingual: list[ReportItem] = Field(default_factory=list)  # 跨語：多語平行交叉印證的候選


class SpanNode(BaseModel):
    span_id: str
    seg_id: Optional[str] = None       # 所屬 chunk（§3 切分）
    category: str = ""                 # 審查者標的類別（§3 分類）
    review: Optional[Review] = None    # 審查判定 正確/存疑/錯字（§3 r1）
    context: str = ""
    history: list[HistoryItem] = Field(default_factory=list)
    is_proper_noun: bool = False
    grounding: Grounding = Field(default_factory=Grounding)
    decision: DecisionInfo = Field(default_factory=DecisionInfo)
    coarse: bool = False               # 粗修（§4.1 步驟②：錨點高信心同音錯，已直接改）
    refined: bool = False              # 精修（§4.1 步驟③：7B 受限驗證改的）
    reports: SpanReports = Field(default_factory=SpanReports)   # 規則證據（聲學 IPA + 局部語意命中）
    rule_hit: bool = False             # 局部語意語音命中：聲學候選 且 局部語意撐住（gemma 判斷前的規則閘）

    @property
    def current(self) -> Optional[str]:
        return self.history[-1].value if self.history else None


# ── §5 Segment（站1 切分後的 chunk＝收斂單位）──────────────────
class Segment(BaseModel):
    seg_id: str
    idx: int = 0
    raw_text: str = ""                 # ＝ v0（收斂的第一版；去標點 core＝下游收斂單位，不重疊）
    normalized: str = ""
    ref: str = ""                      # 同段外語對照（舊欄位，相容保留；新管線改用 langs）
    langs: dict[str, str] = Field(default_factory=dict)   # 26 語 ISO-639-3 平行版本（cmn 為錨；無 ground truth，供跨語交叉驗證）
    # ── 切分站手動校正用（core 連續覆蓋不漏；context 重疊只供顯示，不往下游收斂流）──
    start: int = 0                     # core 在 zh_full 的字元起點（手動劃分定位）
    end: int = 0                       # core 終點
    core_text: str = ""                # core 含標點原文（顯示/手動劃分）
    ctx_before: str = ""               # 前鄰重疊padding（只顯示邊界上下文）
    ctx_after: str = ""                # 後鄰重疊padding（只顯示）
    ground_context: list[dict] = Field(default_factory=list)  # 接地語境錨點（心智圖卡+wiki，不改字；§3.2）
    review: Optional[Review] = None
    span_ids: list[str] = Field(default_factory=list)


# ── §5 來源容器（批次 / 場次）─────────────────────────────────
class Version(BaseModel):
    vid: str
    from_: str = Field("", alias="from")
    text: str = ""
    frozen: bool = False

    model_config = {"populate_by_name": True}


class BatchNode(BaseModel):
    batch_id: str
    domain: str = ""
    domain_prior: str = ""              # 弱語意先驗（2606.10838）
    classification: str = ""            # 跑前手選注入的分類名稱（§3 分類注入）
    meta: dict = Field(default_factory=dict)    # 智慧整理：meeting_date/produced_date/origin/source_kind
    raw: dict = Field(default_factory=dict)     # 站0 入口原文 {text,kind,filename}
    ttt_paths: dict = Field(default_factory=lambda: {"primary": "zh", "aux": ["en"]})
    sources: dict = Field(default_factory=dict)
    segments: list[Segment] = Field(default_factory=list)   # 站1 chunk（切分單位）
    versions: list[Version] = Field(default_factory=list)   # v0 原稿（不覆寫）
    spans: list[SpanNode] = Field(default_factory=list)
    snapshots: list = Field(default_factory=list)           # ground_context 跑前 spans 快照


# ── §5 TermEntry（字典彙整節點）────────────────────────────────
class Occurrence(BaseModel):
    batch_id: str
    wrong: Optional[str] = None
    context: str = ""
    confidence: float = 0.0
    grounding: Optional[str] = None


class TermEntry(BaseModel):
    term_id: str
    correct: str
    type: str = "term"
    lang: str = "cmn"
    ipa: Optional[str] = None
    variants: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    occurrences: list[Occurrence] = Field(default_factory=list)
    confidence: float = 0.0
    updated_at: str = ""


# ── §5 Classification（分類＝具名 LLM 提示詞；跑前注入，§3）────
class Classification(BaseModel):
    class_id: str
    name: str
    prompt: str = ""                    # 分類依據（注入 breeze 的提示詞）
