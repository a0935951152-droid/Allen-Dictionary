"""執行期設定：全部可由 docker-compose / .env 環境變數覆寫。

調參一處集中（.env），不用翻 code。分組：
  ① 服務座標　② 糾正側決策閾值（直接 gate 改字）　③ 召回/檢索/切分
  ④ 接地索引　⑤ 知識側抽取　⑥ 收斂（⚠ 待移除，暫留相容）
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _f(key: str, default: float) -> float:
    return float(os.getenv(key, default))


def _i(key: str, default: int) -> int:
    return int(os.getenv(key, default))


@dataclass(frozen=True)
class Settings:
    # ── ① 服務座標 ────────────────────────────────────────────
    data_dir: str = os.getenv("DATA_DIR", "./data")
    llm_base_url: str = os.getenv("LLM_BASE_URL", "http://vllm:8000/v1")
    llm_model: str = os.getenv("LLM_MODEL", "judge")
    embed_url: str = os.getenv("EMBED_URL", "http://tei-embed:80")
    rerank_url: str = os.getenv("RERANK_URL", "http://tei-rerank:80")
    ner_url: str = os.getenv("NER_URL", "http://ner:80")
    sat_url: str = os.getenv("SAT_URL", "http://sat:80")

    # ── ② 糾正側決策閾值（直接決定改不改字；tune 主戰場）─────────
    ipa_fuzzy: float = _f("IPA_FUZZY", 0.28)             # CAG 音近召回上限
    ipa_exact: float = _f("IPA_EXACT", 0.0)              # 字典/refterms 真同音
    ipa_sub_confuse: float = _f("IPA_SUB_CONFUSE", 0.34)  # 同混淆類音素替換代價（IPA 距離的尺）
    ipa_sub_tone: float = _f("IPA_SUB_TONE", 0.30)       # 聲調 token 互換/增刪代價（A2 聲調入距）
    local_floor: float = _f("LOCAL_FLOOR", 0.35)        # 局部語意命中門檻（rule_hit）
    wiki_floor: float = _f("WIKI_FLOOR", 0.55)          # 語意錨點 wiki 橋接下限（semantic_local/ground_context）
    common_fpm: float = _f("COMMON_FPM", 10.0)          # 通用詞門檻（每百萬詞次數；≥此值＝ambiguous 永不 auto）
    strong_acoustic: float = _f("STRONG_ACOUSTIC", 0.55)  # ⚠ 未接線：舊 refine 遺留，現行 code 不讀
    strong_semantic: float = _f("STRONG_SEMANTIC", 0.42)  # ⚠ 未接線：舊 refine 遺留，現行 code 不讀

    # ── ③ 召回 / 檢索 / 切分 ──────────────────────────────────
    phon_query_limit: int = _i("PHON_QUERY_LIMIT", 8)   # 每詞召回幾個音近候選
    local_topk: int = _i("LOCAL_TOPK", 5)               # 局部語意排名取前 k
    chunk_hi: int = _i("CHUNK_HI", 400)                 # core 字數上限
    chunk_lo: int = _i("CHUNK_LO", 0)                   # core 字數下限（0=auto max(120,hi//2)）
    chunk_ov: int = _i("CHUNK_OV", 40)                  # context 重疊 padding 字數

    # ── ④ 接地索引（心智圖 / wiki 大庫）────────────────────────
    mind_retrieve_k: int = _i("MIND_RETRIEVE_K", 6)     # 每 chunk 取幾個心智圖實體
    mind_topk: int = _i("MIND_TOPK", 5)                 # 局部句取前 k 實體
    know_embed_batch: int = _i("KNOW_EMBED_BATCH", 128)  # 心智圖嵌入批次
    know_wiki_k: int = _i("KNOW_WIKI_K", 3)             # 心智圖→wiki 連結取數
    know_wiki_floor: float = _f("KNOW_WIKI_FLOOR", 0.4)  # 心智圖→wiki 連結下限
    mind_links_floor: float = _f("MIND_LINKS_FLOOR", 0.5)  # graph 連線顯示下限
    mind_links_n: int = _i("MIND_LINKS_N", 20)          # graph 連線取數
    wiki_search_k: int = _i("WIKI_SEARCH_K", 8)         # wiki 檢索取數

    # ── ⑤ 知識側抽取（影響字典/CAG 內容品質）──────────────────
    know_chunk_size: int = _i("KNOW_CHUNK_SIZE", 700)   # 文件切 chunk 字數
    know_whole_chunk: int = _i("KNOW_WHOLE_CHUNK", 1100)  # 整檔模式 chunk 字數
    know_whole_doc_max: int = _i("KNOW_WHOLE_DOC_MAX", 3000)  # 整檔抽取字數上限
    know_gleanings: int = _i("KNOW_GLEANINGS", 1)       # gleaning 補抽趟數
    know_doc_cap: int = _i("KNOW_DOC_CAP", 40)          # 每檔最多 chunk
    know_doc_conc: int = _i("KNOW_DOC_CONC", 8)         # 每檔抽取併發
    know_conf_high: float = _f("KNOW_CONF_HIGH", 0.9)   # 對照表「高」信心
    know_conf_mid: float = _f("KNOW_CONF_MID", 0.7)     # 對照表「中」信心
    know_conf_low: float = _f("KNOW_CONF_LOW", 0.5)     # 對照表「低」信心
    know_t2_conf: float = _f("KNOW_T2_CONF", 0.7)       # LLM Pass2 特殊詞信心
    know_t3_conf: float = _f("KNOW_T3_CONF", 0.5)       # LLM 純語意信心

    # ── 判官併發 ──────────────────────────────────────────────
    judge_concurrency: int = _i("JUDGE_CONCURRENCY", 4)


settings = Settings()
