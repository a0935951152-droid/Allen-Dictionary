# 收斂式字典 — 封閉建構

把 ASR 逐字稿的同音/形近錯字校成正解，閉環回灌成越用越準的字典。**全離線**：模型只從
`/nas-data/allen/models` 唯讀載入、掛 `internal` 網路無對外路由 → 不洩漏會議內容、不打線上 API。

**核心理念（retrieval-then-correct）**：先用 wiki 大庫＋心智圖立**語境錨點**，兩軸並行判讀，再由判官拍板——
偵測(變體掃描+NER+IPA) → 兩軸接地(IPA命中 × 局部語意撐) → 判官(gemma 四態) → 人工。便宜檢索早做且廣、貴的 LLM 晚做且窄。

## 文件（真相源＝code；文件依 code 盤點）

| 文件 | 內容 |
|---|---|
| **[CLAUDE.md](CLAUDE.md)** | **天條/治理鐵律** + 文件索引 + 服務座標（session 入口，先讀這份） |
| [系統架構.md](系統架構.md) | 理念、雙側資料流、翻轉後管線、模組職責、SOTA 對照 |
| [資料結構與資料庫.md](資料結構與資料庫.md) | 節點 schema、`data/` 目錄樹、各落地檔 schema |
| [功能與API.md](功能與API.md) | 端點全表、四道參數、入口 |
| [指令.md](指令.md) | 常用指令手冊（dic / 建置 / 跑管線 / 除錯） |
| [TODO.md](TODO.md) | 進度與待辦 |

## 快速開始

```bash
dic up                 # 收斂棧常開（tei-embed+ner+sat+api；LLM 不常駐）→ 印前端網址、自動起 ops 代理
dic status             # 健康狀態
# DB 建置一輪（Gemma 獨佔窗口，跑完自動換回；前端 graph.html 有一鍵+進度條）
dic build              # 知識庫從頭全抽：sources → Gemma → 心智圖 + CAG（增量→前端按鈕）
dic wiki ; dic mind    # wiki 大庫(hnswlib) + 心智圖嵌入(接地橋)
# 跑一條校正管線（判官走獨佔窗口）
B=evt_250068
curl -fsS -X POST localhost:8080/api/batches/$B/segment
curl -fsS -X POST localhost:8080/api/batches/$B/ground_context
dic judge              # Gemma 判官批次（= 前端「判官出動」按鈕）
curl -fsS -X POST localhost:8080/api/batches/$B/commit
curl -fsS -X POST localhost:8080/api/dictionary/sync    # 三套輸出 → data/correct/sync.json
```
詳見 [指令.md](指令.md)。

## 元件（封閉堆疊，docker compose）

| 服務 | 角色 | 模型 |
|---|---|---|
| `api` | FastAPI（管線/字典/知識庫/前端） | — |
| `tei-embed` | 嵌入（切分邊界 / 接地檢索）——收斂棧唯一 GPU | bge-m3 |
| `ner` | 偵測器（圈專名 span） | ckip-bert |
| `sat` | 句分割（字節級，不靠標點） | sat-3l-sm |
| `vllm-build` | **Gemma 獨佔窗口**（判官/知識庫抽取；`dic build/judge` 或前端按鈕才拉，跑完自動收） | Gemma-26B-A4B AWQ |
| ~~`vllm`~~ ~~`tei-rerank`~~ | ⚠ 已淘汰不啟動（Breeze-7B 判官已由 Gemma 取代；rerank 引擎未用） | — |

模型掛 `models-net`(internal)；只有 `api` 對 host 公開（`:8080`）。

## 前端

`/`(收斂主控台) · `/graph.html`(心智圖 + 🔗wiki 接地圖) · `/cag.html`(CAG 變體管理)。

## 目錄

`services/{api,ner}` 程式；`data/{knowledge,index,wiki,batches}` 落地（見 [資料結構與資料庫.md](資料結構與資料庫.md)）；
`bin/{dic,oburl}` CLI；`scripts/` 建置/煙測。
