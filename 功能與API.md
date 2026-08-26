# 功能與 API

> 天條見 [CLAUDE.md](CLAUDE.md)、流程見 [系統架構.md](系統架構.md)、資料見 [資料結構與資料庫.md](資料結構與資料庫.md)。

## 索引
- [1. 糾正側管線（一條批次的旅程）](#1-糾正側管線一條批次的旅程)
- [2. 接地大庫 / wiki](#2-接地大庫--wiki)
- [3. 純淨側知識庫](#3-純淨側知識庫)
- [4. CAG 變體治理](#4-cag-變體治理)
- [5. 字典 / 分類 / 其他](#5-字典--分類--其他)
- [6. 四道修正參數](#6-四道修正參數)
- [7. 前端 / CLI 入口](#7-前端--cli-入口)

---

## 1. 判定側管線（一條批次的旅程）

兩層架構：**知識側建置**（離線 build 心智圖/CAG/wiki，見 §2/§3）→ **判定側校正**（下列）。
依序呼叫（`{b}`＝batch_id）：

| # | 端點 | 作用 |
|---|---|---|
| 1 | `POST /api/batches` → `POST /api/batches/{b}/ingest`\|`/upload` | 建批次、匯入逐字稿 |
| 2 | `POST /api/batches/{b}/segment` | bge 邊界切 chunk（重切會清舊 span） |
| 3 | `POST /api/batches/{b}/ground_context` | **偵測＋兩軸接地**：變體掃描+NER+IPA 出可疑 span；兩軸並行讀數（IPA命中 × 局部語意撐）；心智圖實體→wiki 橋寫 `seg.ground_context`（不改字）；`rule_hit`＝兩軸皆撐 |
| 4 | `POST /api/ops/judge`（推薦）｜`POST /api/batches/{b}/coarse_fix`（單批直接判斷） | **判官（Qwen 常駐服務）**：吃兩軸證據＋整段接地錨點＋平行多語 → `{abandon,xling}` → 四態（①修 ②維持 ③人工 ④—）。ops 走主機代理：確認 Qwen 健康→`/api/judge_pending` 批次判所有存疑，其餘服務不中斷；已判 human 的 span 不重擲骰。**常用原詞永不 auto（封頂人工）** |
| 5 | `POST /api/batches/{b}/refine` | **人工清單**：第④態（規則沒撐、判官靠跨語救回）列人工裁決 |
| 6 | `POST /api/batches/{b}/commit` → `POST /api/dictionary/sync` | 併入字典（只併 decision=auto；**通用詞不進 variants**、confidence 以 distinct 批次計）→ sync 分 **regex**（可盲替換）/**contextual**（不得盲替換）/ipa·tts(pending)。前端 ⑥「回灌/匯出」站視覺化 |

檢視：`GET /spans`、`/spans/{sid}`。偵測單獨重跑：`POST /review`。
人工：`PATCH /spans/{sid}`（改字/裁決，by=human）、`PATCH /meta`。

## 2. 接地大庫 / wiki

| 端點 | 作用 |
|---|---|
| `POST /api/wiki/ingest` | 解析 `data/wiki/raw` dump → `entries.jsonl`（CPU；或 CLI `python -m app.engine.wiki_ingest`） |
| `POST /api/wiki/reindex` | entries → bge → hnswlib（長跑；建議 CLI `python -m app.engine.wiki_index`） |
| `GET /api/wiki/stats` | 索引狀態（就緒/條目數/維度） |
| `GET /api/wiki/search?q=&k=` | 單一 query 檢索 wiki（前端點節點查 grounded wiki） |
| `POST /api/wiki/links` | 批次 `{terms,k,floor}` → 各實體 grounded wiki（前端 wiki 接地圖，上限 120） |
| `POST /api/knowledge/embed_mind` | 嵌入心智圖實體向量（接地橋；增量）`GET /api/knowledge/mind_stats` |

## 3. 純淨側知識庫

| 端點 | 作用 |
|---|---|
| `POST /api/ops/build` | **前端一鍵建庫（推薦）**：排入 ops 佇列 → 主機 `dic agent` 開 Qwen 常駐服務執行 → `GET /api/ops/status` 輪詢進度（前端進度條）。body `{clear:bool}` |
| `POST /api/knowledge/build` | sources → 兩階段 Qwen 抽取 → vault+CAG+acoustic（增量；`clear`重整）。**需完整常駐棧健康**（ops 版免操心） |
| `POST /api/knowledge/relabel` | 重貼類別（不重抽） |
| `GET /api/knowledge/graph`,`/categories` | 心智圖 graph JSON / 類別計數（前端） |
| `GET /api/knowledge/sources`,`POST /upload`,`/decide` | 來源清單 / 上傳 / 逐檔抽取決策 |
| `GET /api/knowledge/stats` | 知識庫儀表板 |

## 4. CAG 變體治理

| 端點 | 作用 |
|---|---|
| `GET /api/cag/variants?q=&only_special=` | 列 CAG 詞+變體（變體多的排前，找雜訊） |
| `PUT /api/cag/variants/{term}` | 覆蓋式設變體 → 改 vault md（`locked:true`，build 不沖）+ 刷聲學索引 |

> 前端 `/cag.html`。修髒變體（如「有形」誤掛 現在/未來）即時消除偵測端誤召。

## 5. 字典 / 分類 / 其他

`GET /api/dictionary/terms`、`PATCH /api/dictionary/terms/{id}`、`POST /api/dictionary/sync`（三套輸出，落檔 `data/correct/sync.json`）；
`POST /api/ops/{build|judge}`＋`GET /api/ops/status`（ops 佇列）；`GET /api/pipeline/params`（參數視覺化，唯讀）；
`GET/POST/PATCH/DELETE /api/classifications`；`GET /health`；`GET/DELETE /api/batches/{b}`。

## 6. 修正參數（實際生效值看前端「⚙ 參數」站或 `GET /api/pipeline/params`；調整走 .env → `dic api`）

| 道 | env 鍵（皆已接 `settings.*`） | 預設 | 作用 |
|---|---|---|---|
| IPA-CAG 模糊 | `IPA_FUZZY` | 0.28 | CAG 容未登錄音近 |
| IPA-真同音 | `IPA_EXACT` | 0.0 | 字典/refterms 只收完全同音（**含聲調**——真同音須含調全同） |
| 聲調代價 | `IPA_SUB_TONE` | 0.30 | 聲調 token 互換/增刪（還是↔海蝕 d=0.15 非滿分） |
| 混淆類代價 | `IPA_SUB_CONFUSE` | 0.34 | 送氣/捲舌/前後鼻音互換 |
| 局部語意撐 | `LOCAL_FLOOR` / `LOCAL_TOPK` | 0.35 / 5 | chunk→心智圖餘弦下限（rule_hit；排名制前 k） |
| 接地-wiki | `WIKI_FLOOR` | 0.55 | 實體→wiki 相似下限 |
| 通用詞門檻 | `COMMON_FPM` | 10 | jieba 詞頻/百萬詞 ≥此值＝通用詞：變體 ambiguous、原詞永不 auto、不進 regex |
| 判官併發 | `JUDGE_CONCURRENCY` | 4 | 對應 llama.cpp 4 slots |

> 登錄變體（人工核可的確定誤聽）＝verified，**不受 IPA 門檻** 必收（天條4）；
> **例外＝通用詞變體（ambiguous）**：只以真實 IPA 距離提名、證據標「曾登錄誤聽樣態(需語境確認)」、判定封頂人工。

## 7. 前端 / CLI 入口

- 前端：`/`(判定主控台：四象限、⑥回灌/匯出、⚙參數)、`/graph.html`(心智圖 + wiki 接地圖 + 一鍵build/嵌入)、`/cag.html`(CAG 變體管理)。
- CLI：`bin/dic`(操作)、`bin/oburl`(LAN 網址)；大庫建置 `python -m app.engine.{wiki_ingest,wiki_index}`。
- 啟動：`dic up`（收斂棧）；改 api `dic api`。詳 [指令.md](指令.md)。
