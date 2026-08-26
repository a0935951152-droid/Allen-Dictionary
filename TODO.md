# TODO / 進度

> 文件總則見 [CLAUDE.md](CLAUDE.md)。架構以 code 為真相，本檔只記進度與待辦。
> 現役兩層：**知識側建置**（心智圖/CAG/wiki）＋**判定側校正**（偵測→兩軸接地→判官四態→人工）。

---

## ✅ 操作凍結已解除（2026-07-03 E2E 驗收通過）

原凍結：A1＋A2 落地前不對 evt_250068/331072 跑 judge/commit（防「還是→海蝕」進全域 regex）。
**E2E 全鏈驗收結果**（build→mind→ground_context→judge(Qwen 獨佔)→commit→sync）：
- 高頻詞 auto 修＝**0**（41 個「還是」全維持）；「還是」滿分變體證據＝0（現 d=0.15/score 0.46/需語境確認）。
- 判官：268 存疑 → 修 48／維持 219／人工 1；48 個 auto 全為 junk→正解（龍山是→龍山寺、沼井→藻井…）。
- gold 8 正修 **7/8**（相擁→鄉勇 這輪被 Qwen abandon＝召回小退；相擁 freq 0.15 非 common，非 cap 所致）。
- sync：regex 38 條乾淨、contextual 0、「食材/還是」皆未入 variants。
⚠ **殘留風險（新凍結對象）**：「**食材↔石材**」案例——ASR 正確詞被提名（含調 d=0.0 真同音＋單域語意
飽和 rule_hit=True），**唯一防線剩 Qwen**；jieba freq(食材)=0、wiki 無獨立條目 → A1/A3/A5 守門全罩不到。
→ 對「真同音（d=0）且原詞成詞」的 span，判官前應加 cap=human（見 A3 升級），落地前 auto 修結果需人掃一遍再 commit。

---

## ✅ 已完成

### 2026-06-24（wiki 大庫接地翻轉 + IPA 統一）
- **retrieval-then-correct**：先接地建語境再修（廢原「CAG 精修在前」）。
- **wiki 大庫**：zhwiki 標題+redirect（~1.5M）→ bge-m3 → hnswlib（`wiki_ingest`/`wiki_index`，增量）。
- **心智圖→wiki 橋**：chunk → 相關心智圖實體（乾淨語境）→ 以實體當 query 查 wiki（grounded）→ `seg.ground_context` 兩源錨點。**已棄離線全量連結**（噪音 AI→醫生遊戲）。
- **IPA 全面統一**：拆 pinyin_key，聲學/知識側同音群全走 IPA（`word_distance` 逐字抗稀釋）。
- **CAG 變體前端 CRUD** `/cag.html`、**wiki 接地圖** graph.html。
- **文件重構**：CLAUDE.md（天條）+ 系統架構/資料結構/功能與API/指令.md。

### 2026-06-30（NMK 移除 + 兩軸並行 + 判官四態）
- **判定側＝兩軸並行**：IPA命中 × 局部語意撐 為**獨立量測**（非先後）；`reports.semantic_local` 對每候選給讀數（撐/未撐）；前端**四象限視覺化**（① 修 ② 維持 ③ 人工 ④ 無訊）。
- **登錄變體 verified 必收**（bug 修，天條4）：`phonetic_index.variant_correct`，聲學軸 score=1.0 不受 IPA 門檻（龍山是→龍山寺 IPA 0.67 被誤殺 → 修好；evt_250068 rule_hits 30→44 全是 recovered 變體，無 regression）。
  ⚠ 2026-07-02 審查判定此機制**對通用詞變體過度**（見 A1）——`龍山是` 類不成詞字串正確，`還是` 類高頻詞先驗相反。
- **語意接 wiki**（規格 §3.2/§5）：anchor = mind(term+variants) ∪ wiki(title+aliases)；`know_index.ground_context` 寫 `seg.ground_context`（相擁→鄉勇 靠 鄉勇團 wiki 救回）。
- **判官四態**（取代舊粗修 coarse.py + 精修）：`refine.judge_span` Qwen `{abandon,xling}` → ①修 ②維持 ③人工 ④—；`dic judge` Qwen 常駐服務（`/api/judge_pending` 批次判）。
- **NMK 收斂引擎徹底移除**：刪 7 模組（`converge/route/align/ground/phonetics/diagnose/generate`）、11 端點、config/.env/schema 相關鍵；死碼清除。全文件對齊兩層。煙霧測試（新批次 + 野柳 evt_331072 59chunk）端到端通過。

### 2026-08-25（正式切至 Qwen Q2 常駐架構）
- `qwen + tei-embed + ner + sat + api` 五個核心服務同時常駐；build 與判官共用
  **Qwen3.8-27B Q2_K_L**（llama.cpp，4 slots × 32K）。
- 舊 LLM 與 cross-encoder reranker 已從 Compose、環境變數、health、CLI 與前端啟動鏈移除。
- ops 仍採檔案佇列：`/api/ops/{build|judge}` → 主機 `dic agent` → 常駐 Qwen 直接執行；
  同類操作單工，其他模型服務不中斷。
- RTX 3090 Ti 實測：Qwen + bge-m3 同駐約 18.3GiB；四路短請求可同時使用 slot 0–3，無 OOM。

### 2026-07-03（A 段守門落碼 + 參數接線 + 參數視覺化）〔詳見各待辦項 🔧 註記〕
- **A2 聲調入距**（含 debug：`_TONES` 集合、分母只算音段）＋ **A1 變體分級**（`engine/lexicon.py` jieba 詞頻）
  ＋ **A3 退化版**（常用原詞封頂人工）＋ **A5 commit/sync 守門** ＋ **A8 判官中性化**——全落碼，
  py_compile 過、距離數學單測 8/8 過；**端到端驗證待起服務**（凍結維持）。
- **B9 判定側參數接線**：判定路徑上的引擎常數全改讀 `settings.*`（詳 B.9 🔧）。
- **⚙ 參數視覺化（唯讀）**：`GET /api/pipeline/params`（五階段×實讀參數×公式＋聲學/心智圖現場統計）；
  前端 index.html 加「⚙ 參數」站（不需選批次）。只顯示不調動；調參仍走 .env → 重啟 api。
- **判官重擲骰 bug 修**：`judge_ep` targets 排除 `decision.to=='human'`——第④態已進人工佇列的 span
  重跑 `dic judge`/coarse_fix 不再被 Qwen 重判翻案（人工佇列不流失）。

### 2026-07-01（偵測側深度 + G2P 派發）〔原待辦 A.1/A.3/A.4，已落碼〕
- **第三偵測器 `scan_terms`**（`phonetic_index.py:246`）：CAG 正解詞表直掃 chunk（登錄變體簡繁直掃 + 漢字 IPA 鄰域滑窗召回），繞過 ckip 漏抓（生核化石→生痕化石）。`review.review_detect_segment` A 路。
- **標記剝離 `_clean_for_detect`**（`review.py:24`）：偵測前剝 `⚠️`、`(?…)` 註記。
  ⚠ 2026-07-02 審查判定**剝離不完整**（見 A6）——「錯字⚠️正解」格式的正解詞仍留句內。
- **變體掃描容簡繁**：`scan_variants` flatten 後比對（深恒↔深恆、岩↔巖）。
- **G2P 派發器**（`ipa.py`）：漢字走 pypinyin、其餘書寫系統走 espeak-ng、日文 pykakasi 兩段；共同語言＝IPA。

---

## ⬜ 待辦

### A. 常見詞誤修根因鏈（2026-07-02 結構審查；最高優先）

> 卡點：`還是`/`等等` 類高頻詞被提名滿分、域內句 `rule_hit=True`，唯一防線剩 Qwen 單 bit。
> 審查結論：**清 CAG 髒變體治標無效**（A2 說明原因）；哲學缺口＝候選有接地、**原詞從未被接地**。
>
> **分階段（依賴＋交付門檻，詳 [審查.md](審查.md) 執行規劃節）**：
> - **P1＝A1＋A2**（綁死，不可只做一個）：斬兩條滿分捷徑。只做 A1 → R2 無聲調 d=0 讓 `scan_terms`
>   重新提名滿分繞過；只做 A2 → 登錄變體層仍無條件必收。
> - **P2＝A5**：斷 commit/sync 放大器（`is_common_word` 與 A1 共用 `engine/lexicon.py`）。
>   **P1＋P2 到位＝凍結令可正式解除**；此前 evt_250068/331072 一律不跑 `dic judge`/`commit`。
> - **P3＝A6**（可與 P1/P2 並行）：建 gold 評測集，讓 P4 的 δ/margin 用資料擬合而非手拍。
> - **P4＝A3＋A4**（前置 P3）：原詞對稱接地＋去先驗飽和 → 誤修率趨 0。
> - **P5＝A7＋A8**（前置 P1）：span offset 逐出現＋judge 去偏置，收尾。
> - **最小驗證切片**：先只落 A2 的 `word_distance`，驗「還是↔海蝕 d>0 且八個正修不掉」再往上疊。

1. **變體分級：ambiguous 變體永不 auto**〔R1〕
   `海蝕.md variants:[還是,…]`（觀光署錯誤樣態表）記錄的是「某句曾誤聽」＝**條件事實**，
   `variant_correct`/`scan_variants` 卻升級成「任何語境都是誤聽」＝score=1.0 必收（`reports.py:52`）。
   改：變體本身若是通用詞（`harvest._STOP`／頻率表 zipf／本身是字典詞或 wiki 詞條）→ 標
   `ambiguous`：不進 `variant_map` 直掃必收層（`phonetic_index._build`）、證據不得寫「確定誤聽」、
   判定封頂第④態人工。不成詞字串（龍山是）維持現制。
   驗收：兩批所有 `還是` span 不再出現 score=1.0 變體證據；龍山是→龍山寺 recovered 數不減。
   🔧 **2026-07-03 已改碼待驗證**：新增 `engine/lexicon.py`（**jieba 內建詞頻表**當 zipf 來源——
   已在 requirements、離線查表符天條，免另備詞頻檔；`to_simp` 後查 `jieba.dt.FREQ`，門檻
   `settings.common_fpm=10`/百萬詞，compose/.env.example 已補鍵）。`phonetic_index._build`
   變體分級：`is_common_word(v)` 或 v 本身是字典正解 → 進 `ambiguous` map（不直掃必收），
   其餘進 `variants`；新 API `variant_ambiguous()`。`reports.acoustic` 對 ambiguous 候選加註
   「曾登錄誤聽樣態(需語境確認)」（走 IPA 真實分數，不置頂）。
   📝 **與規格偏差**：ambiguous 採 **runtime 判定**（`_build` 時算），不落 vault frontmatter——
   單一真相源、免資料遷移、新變體自動適用；cap=human 落在 `refine.judge_span`（見 3）而非 span 欄位。

2. **聲調入距：真同音層只收含調全同**〔R2；A1 的必要搭配〕
   `ipa.py:91` 無聲調拼音 → 還(hái)↔海(hǎi)、是(shì)↔蝕(shí) `word_distance=0`「真同音」。
   後果：**就算手清變體，`scan_terms` 滑窗 d=0 ≤ 0.28 照樣重新提名、分數照樣滿分**
   （`reports.py:59` score=1−d/thr=1.0）——這就是「清資料後問題還在」的原因。
   改：聲調差列小代價（~0.15–0.3/音節，比照 `_SUB_CONFUSE` 思路）；真同音層（d=0）只收含調全同；
   CAG 模糊層 0.28 是否連動需重測。
   ⚠ **實作坑**：`Style.TONE3` 拼音尾帶數字（`hai2`），但 `_split`/`_WHOLE`/`_FINALS` 全預期無數字音節 →
   直接餵會解析失敗退 fallback。作法：`_han_tokens`（`ipa.py:91`）**先剝尾部聲調數字 → 現有邏輯處理 base
   音節 → 再 append 聲調 token（T1–T5，輕聲獨立 token）**。改完先單測 `word_distance` 再動 `_CONFUSE`。
   驗收：`word_distance(還是,海蝕)>0`；evt_250068 八個既有 auto 正修（三川殿/藻井/鎮瀾宮/鄉勇/對場作/燈節/刈包/三川店）全數保留。
   🔧 **2026-07-03 已改碼＋數學驗證通過**：`ipa.py` `_han_tokens` 改 `Style.TONE3, neutral_tone_with_five=True`
   → 剝尾數字 → base 音節查表 → append `T1–T5`；`_sub_cost` 聲調互換＝`_SUB_TONE 0.30`。
   debug 修兩處：① 聲調判定改明確集合 `_TONES`（前綴 `T` 會誤中 espeak fallback 的字面 `'T'`）；
   ② **距離分母只算音段 token**（聲調若進分母＝同調對全體變近、0.28 隱性放寬；改後同調對
   與改動前逐位一致：是↔寺 0.67 不變、還是↔海蝕 0→0.15、score 1.0→0.46 → **0.28 免重測**）。
   聲調增刪（跨書寫系統）亦 0.30。手工 token 單測 8/8 過（distance 純函數不需 pypinyin）。
   ⏳ 端到端驗證待容器：真 pypinyin 下 `word_distance(還是,海蝕)=0.15`＋八個正修不掉。
   ℹ 重啟 api 即生效（IPA 全在 runtime 算；`ipa_index.json` 只寫不讀）；vault frontmatter `ipa:` 標籤重建後會多調號 token（純顯示）。

3. **原詞對稱接地：rule_hit 改 margin 制**〔R3〕
   原詞在整條管線從未被接地，唯一承擔者是 Qwen `abandon` 一個 bit（`refine.py:76`）＝單點故障；
   一批 23 個同 surface span＝23 次獨立擲骰。
   改：原詞也算一份語意成立度，`rule_hit` 改 margin（候選撐度 − 原詞成立度）；原詞為強客觀詞
   （字典詞/wiki 詞條/高頻）時抬高改字門檻。這是「客觀語意接地」哲學的對稱補全。
   🔧 **2026-07-03 退化版已改碼**：`refine.judge_span` 在①修之前加
   `lexicon.is_common_word(original) → _manual`＝**常用原詞永不 auto、封頂第④態人工**（一行擋整類錯誤）。
   完整 margin 制（中頻詞候選須高出原詞成立度）仍待 P4（gold 集擬合，見 6）。

4. **排名制加基線對比**〔R4〕
   `top_terms` k=5、卡池 198 張且單域 → 域內句 top-5 恆含候選＝閘門常開。
   實證：evt_250068 講**花枝丸**的句子撐 海蝕 0.46；evt_331072「瓊麻**還是**龍舌蘭」「海巡艦艇**還是**海關艦艇」（明顯連接詞）rule_hit=True。
   改：候選 cos 須顯著高於該句對全卡池的基線（中位數＋margin 或 z-score），不是只擠進 top-k。
   注意：排名制當初是為了解「bge 中文基線 0.35–0.47 絕對門檻無鑑別力」，基線對比是它的正確一般化，不是回到絕對門檻。

5. **commit/sync 守門：斷放大器**〔R5〕
   `main.py:293` auto 修的原詞 append 進 `entry.variants`；`main.py:349` sync 把**所有** variants
   吐成無條件 regex 全域替換；`main.py:305` confidence 以 **span 數**計（一批 8 個誤修 → 0.99
   「高信心」，頻率被誤當佐證）。
   改：ambiguous 變體不進 `dictionary.variants`、sync regex 排除 ambiguous；confidence 改以
   **distinct batch 數**計。判定期 per-context 的結論，儲存期不得壓扁成無條件規則。
   🔧 **2026-07-03 已改碼待驗證**：`commit` append variants 前過 `lexicon.is_common_word(cur)`
   （occurrence 照記留審計）；confidence 改 `{o.batch_id}` distinct 批次數計；`sync` 把
   「通用詞變體／本身是字典正解的變體」改列 `contextual` 清單（回應多一鍵，下游不得盲替換），
   其餘照吐 `regex`。

6. **⚠️ 註記完整剝離 ＋ 轉 gold 評測集**〔R6〕
   原稿註記格式是「錯字⚠️正解」，`_DETECT_MARKERS`（`review.py:24`）只剝 ⚠️ 符號與 `(?…)`，
   **正解詞留在句內** → 局部句字面含「海蝕」→ 語意 cos 虛高（0.67 那句即是）；現有 rule_hit 統計被灌水。
   改：攝取端把「錯字⚠️正解」解析成 gold pair 另存旁路（**現成評測集**，量測 A3/A4 用）；
   偵測/嵌入文本剝到只剩錯字原文。
   驗收：剝離後重跑 ground_context，比對 rule_hits 消長；gold pair 產出 ≥ 兩批全部註記數。

7. **span 帶 offset、同 surface 逐出現判定**〔R7〕
   `scan_terms` 有 `start`，`_mk_span`（`review.py:82`）丟棄；`seen_surface` 去重 → 一個 chunk
   5 個「還是」只建 1 個 span；`local_sentence`（`reports.py:41`）又取**第一個**含詞句 →
   判的句子可能不是有問題的那個出現位置，修字也無法只改該處。常見詞下此設計不成立。
   改：SpanNode 帶 offset、逐出現建 span（或至少逐出現各判），`local_sentence` 用 offset 取句。

8. **judge 證據中性化 ＋ 餵 `seg.ground_context`**〔R8；併原 B.5〕
   `refine.py:73` 證據行寫「海蝕(**變體(確定誤聽)**)」＋「候選**正解**」＝提示詞先告訴判官答案。
   改：中性措辭（「曾登錄誤聽樣態」「候選」）；prompt 加整塊語境錨點 `seg.ground_context`
   （木構造句可見「整塊零海蝕訊號」）。風險低（只改措辭＋加輸入）。
   🔧 **2026-07-03 已改碼**：`refine.judge_span` 措辭「候選正解→候選詞」；新增 `_render_ground(seg)`
   把心智圖實體＋橋接 wiki 攤成「本段接地錨點」餵進 prompt；abandon 指引加「原詞為常用詞／原句通順／
   錨點與候選無關 → abandon=true」。⚠ 聲學證據標籤「變體(確定誤聽)」在 `reports.acoustic`，隨 A1 ambiguous 一起改。

### B. 判官精修

9. **參數外移 #3**：判官/引擎閾值仍讀 module 常數，調 .env 靜默無效（見 [審查.md](審查.md)）。
   🔧 **2026-07-03 判定側已接線**：`_IPA_FUZZY`/`_IPA_EXACT`/`_SUB_CONFUSE`/`_SUB_TONE`（ipa/phonetic_index）、
   `_LOCAL_FLOOR`/`_WIKI_FLOOR`/`semantic_local k`（reports）、`JUDGE_CONCURRENCY`（refine）、
   `ground_context` 預設（know_index）、`COMMON_FPM`（lexicon）→ 全改讀 `settings.*`；config 補
   `ipa_sub_tone`/`wiki_floor` 鍵；compose api env 補 9 鍵（`${VAR:-預設}` 形式）＋ .env.example 補段。
   `STRONG_ACOUSTIC/SEMANTIC` 確認為舊 refine 遺留死鍵，config 已標 ⚠未接線。
   ⬜ 剩：③召回其餘（chunk_*/phon_query_limit 部分）與 ⑤知識側組逐一對賬。
10. **per-span IPA 否決關卡**：語意召回的讀音守門（豆腐沿→豆腐岩留、→豆腐腦否決）。

### C. 知識側資料品質（garbage-in）

11. **心智圖雜訊實體**：`授權給他`/`（爭的）那些東西` 等抽取雜訊 → 抽取端或偵測端過濾。
12. **CAG 髒變體手清**：`有形←現在/未來/有效`、`海蝕←還是`、`薑石←講是/講石`、`象石←響是/向是` 類。
    ⚠ **僅治標**：同音提名靠 A1（分級）＋A2（聲調）才斷根，手清後仍會被 `scan_terms` 滑窗重新提名。
    手清價值＝去掉「確定誤聽」這個錯誤標籤，仍值得做，但不要期待它解掉誤修。
13. **變體半自動擴充**（原 A.2，未做）：生痕化石只登 `深恆化石`，缺 `生核化石/生物核化石/深恒化石`(簡)；
    從歷史誤聽/gold pair（A6 產出）回灌，人工核可後入庫。
    ℹ 2026-07-03 查證：wiki 庫**有**此詞（主標題「遺蹟化石」，`生痕化石` 在 aliases；語意搜 0.8 命中）、
    心智圖卡 wiki 橋接良好（遺蹟化石 0.793）。缺口純在偵測端變體表，不在知識庫。

### D. 門檻 / 基建

14. **接地門檻跨域調校**：`_LOCAL_FLOOR 0.35`/`_WIKI_FLOOR 0.55`/`judge conf 0.5` 跨多批實測（目前單批）；
    A4 落地後基線對比參數一併納入。
15. **wiki 大庫增量自動化**：心智圖長大自動補連、新 dump 增量 add_items（hnswlib 已支援，缺排程）。
16. **wiki 摘要版（v2，可選）**：目前標題+redirect；摘要需處理 3.3GB `pages-articles` dump，語意消歧更強。
17. **wiki 搜尋 alias 回填主標題（前端體感）**：`wiki_index.search` 命中 alias 時（如「生痕化石」）
    已能回主條目「遺蹟化石」＋顯示 aliases，但前端若做標題精確比對會誤判「查無」。確認前端接地圖/搜尋
    以語意分數為準、alias 命中標示「（別名←生痕化石）」，避免使用者誤以為缺詞。

---

## 驗收基準（A 段完工定義）

- **誤修率**：兩批所有高頻詞（還是/等等/就是/現在…）**零 auto 修**；只允許第④態人工建議。
- **召回不退**：evt_250068 八個 auto 正修全保留；rule_hits 中真變體 recovered（龍山是 類）不減。
- **評測集**：A6 gold pair 落地，之後任何門檻調整都跑同一套（取代單批肉眼驗證）。

---

## 已知資料 / 環境

- `evt_250068`＝龍山寺/地質導覽（萬華，67 span）；`evt_331072`＝野柳地質導覽（59 chunk、64 span，稿含「錯字⚠️正解」與 `⚠️(?…)` 人工標記，多語平行）。兩批「還是」存疑 span 見頂部凍結令。
- 知識側＝王道經營＋觀光署地質，部分跨域；CAG 108 詞（acoustic.json is_special；vault 198 卡）、wiki 1,537,715 條目。
- wiki dump：abstract dump 已停更，摘要只能從 `pages-articles`（3.3GB）抽。
- TEI `max-client-batch-size` 已調 256（bulk 嵌入）。
- **備份**：refactor 前 app/.env/docs 備份在 scratchpad（no-git 保險）。
