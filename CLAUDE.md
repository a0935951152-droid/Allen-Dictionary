# CLAUDE.md — 收斂式字典專案總則

收斂式字典：把 ASR 逐字稿的同音/形近錯字，靠「**先接地建語境、兩軸並行判讀、再由判官拍板**」校成正解，
閉環回灌成越用越準的字典。**兩層**：知識側建置（心智圖/CAG/wiki 大庫）＋判定側校正（偵測→兩軸接地→判官四態→人工）。
FastAPI(`services/api`) + ckip NER(`services/ner`)，docker compose；模型全離線（Qwen3.8-27B Q2_K_L 判官/建庫共用、TEI bge-m3）。

---

## 天條（不可違反 — 工程約束）

1. **MISA（規則式聲學）**：聲學/正規化一律**規則式**（IPA `ipa.word_distance` 逐字距離）、純 CPU、
   零訓練、零安裝、零上網。學習式聲學嵌入（Apple ANE）雖更準但需訓練 → **不採用**，規則 IPA 即終態。
2. **封閉建構**：模型只從本地路徑載入、掛 `models-net`（internal，無 gateway）→ 不可能下載/外洩。
   只有 `api` 對 host 公開。wiki dump 等外部語料只在**主機**離線下載一次，容器不連外。
3. **降級不壞**：語意服務（bge/Qwen）不可用時音韻仍可離線接地；缺 pypinyin/hnswlib/opencc → no-op，不崩。

## 治理鐵律（商業不能錯）

4. **candidate vs verified**：LLM 抽的、wiki 撈的**一律 candidate**；只有 **verified**
   （對照表 gold ＋ 人工核可 `locked`）才驅動校正、升 gold、進乾淨 CAG / 聲學索引。
5. **wiki 永不自動升 gold**——只當語境錨點。**人類擁有最終字典**。
6. **鎖定不覆寫**：vault 詞 `locked:true` → 知識庫重建保留人工版（CAG 變體前端 CRUD 即靠此）。
7. **寧缺勿濫**：接錯成本 > 漏接；門檻不過 → 不接、保持原樣 / 交人工。
8. **資料佈局分離**：純淨側（知識庫 `knowledge/`）≠ 糾正側（逐字稿 `index/`,`batches/`）。
   純淨側只收 authored 乾淨源；逐字稿/字幕/ASR 校正排除（含錯字會污染知識側）。

> 改任何東西前先確認沒違反以上。新增聲學手段先問「規則式嗎？離線嗎？」；
> 新增自動改字先問「來源是 verified 嗎？」；新增模型呼叫先確認在 `models-net` 內。

---

## 文件索引

| 文件 | 內容 |
|---|---|
| **[系統架構.md](系統架構.md)** | 核心理念、雙側資料流、判定側管線（偵測→兩軸接地→判官四態→人工）、模組職責、SOTA 對照 |
| **[規格.md](規格.md)** | 演算法級規格（按管線塊）；①知識側：收什麼/用什麼收/怎麼驗證/對照表格式 |
| **[資料結構與資料庫.md](資料結構與資料庫.md)** | 節點 schema（Batch/Segment/SpanNode）、各落地檔 schema、`data/` 目錄樹 |
| **[功能與API.md](功能與API.md)** | 端點全表（依管線分組）、四道參數、CLI/前端入口 |
| **[指令.md](指令.md)** | 常用指令手冊（`dic` 服務/建置、跑管線 curl、CAG 管理、除錯） |
| [README.md](README.md) | 部署/啟動/操作 |
| [TODO.md](TODO.md) | 進度與待辦 |

舊單檔 `活動字典閉環優化架構.md` 已拆分至上列，僅留作歷史對照。

---

## 服務座標

- `dic up`＝完整常駐棧（qwen / tei-embed / ner / sat / api）；判官與建庫直接共用 Qwen（`dic judge`/`dic build` 或前端按鈕）。`bin/oburl` 顯示 LAN 網址。
- 端口：api `:8080`。前端：`/`(主控台)、`/graph.html`(心智圖+wiki接地圖)、`/cag.html`(CAG 變體管理)。
- 模型：`MODELS_ROOT=/nas-data/allen/models`；judge=Qwen（`QWEN_MODEL_PATH`，常駐 Qwen）、embed=bge-m3。⚠ 舊 LLM 服務與 reranker 已淘汰不啟動。
- **修正參數**（實際生效值看前端「⚙ 參數」站；調整走 .env → `dic api`）：IPA CAG≤0.28 / 字典·refterms≈0（含聲調，真同音須含調全同）；局部語意≥0.35(排名前 k=5) / wiki≥0.55；判官四態(abandon×xling)。登錄變體＝verified 必收(不受 IPA 門檻)；**通用詞變體=ambiguous 例外**（jieba 詞頻≥COMMON_FPM，永不 auto、封頂人工）。
- 重模型服務改參數要 `docker compose up -d <svc>` 重啟；改 api 程式 `docker compose up -d --build api`。
- **不要停 `gitlab-runner` 容器**（非本專案）。
