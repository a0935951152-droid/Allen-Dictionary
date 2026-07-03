"""模型服務 client：判官 LLM(vLLM)、嵌入/重排(TEI)。皆走容器內部網路。"""
from __future__ import annotations

import httpx

from .config import settings

_timeout = httpx.Timeout(120.0, connect=10.0)


# ── 判官 LLM（vLLM OpenAI 相容）────────────────────────────────
async def judge_chat(prompt: str, *, max_tokens: int = 128, temperature: float = 0.0) -> str:
    async with httpx.AsyncClient(timeout=_timeout) as c:
        r = await c.post(
            f"{settings.llm_base_url}/chat/completions",
            json={
                "model": settings.llm_model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()


# ── 嵌入（TEI /embed）──────────────────────────────────────────
async def embed(texts: list[str]) -> list[list[float]]:
    async with httpx.AsyncClient(timeout=_timeout) as c:
        r = await c.post(f"{settings.embed_url}/embed", json={"inputs": texts})
        r.raise_for_status()
        return r.json()


# ── 重排（TEI /rerank）─────────────────────────────────────────
async def rerank(query: str, texts: list[str]) -> list[dict]:
    """回傳 [{index, score}, ...]，已依 score 由高到低排序。"""
    async with httpx.AsyncClient(timeout=_timeout) as c:
        r = await c.post(
            f"{settings.rerank_url}/rerank",
            json={"query": query, "texts": texts, "return_text": False},
        )
        r.raise_for_status()
        return r.json()


# ── NER（§3 篩選：本地 Chinese NER 抽候選專名）─────────────────
async def ner(text: str) -> list[dict]:
    """回傳 [{word,type,score,start,end}, ...]＝完整實體（ner 服務端已做 BIOES 精解析，相鄰正確切分）。"""
    async with httpx.AsyncClient(timeout=_timeout) as c:
        r = await c.post(f"{settings.ner_url}/ner", json={"text": text})
        r.raise_for_status()
        return r.json().get("entities", [])


async def sat_split(text: str) -> list[str]:
    """SaT 字節級切句（多語、不靠標點）→ 句子清單。失敗拋例外，呼叫端降級規則切（天條 3）。"""
    async with httpx.AsyncClient(timeout=_timeout) as c:
        r = await c.post(f"{settings.sat_url}/split", json={"text": text})
        r.raise_for_status()
        return r.json().get("sentences", [])


async def embed_alive() -> bool:
    """探 embed 服務是否在線（重嵌入前預檢，避免離線時卡到 read timeout）。3s 內無回應即視為離線。"""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as c:
            return (await c.get(f"{settings.embed_url}/health")).status_code == 200
    except Exception:
        return False


async def health() -> dict:
    """模型服務健康狀態，供 /health 回報。"""
    out: dict[str, bool] = {}
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as c:
        for name, url in (
            ("llm", settings.llm_base_url.rsplit("/v1", 1)[0] + "/health"),
            ("embed", f"{settings.embed_url}/health"),
            ("rerank", f"{settings.rerank_url}/health"),
            ("ner", f"{settings.ner_url}/health"),
            ("sat", f"{settings.sat_url}/health"),
        ):
            try:
                resp = await c.get(url)
                out[name] = resp.status_code == 200
            except Exception:
                out[name] = False
    return out
