#!/usr/bin/env bash
# 離線端到端煙霧測試：建批→生成→收斂→接地→分流→彙整→sync。
# 用 §5 的 豆腐巖→豆腐岩 範例，外加常用詞(直接過) 與 宏雞(搖擺→人工)。
set -euo pipefail
API="${API:-http://localhost:8080}"
B="evt_smoke_geology_01"
j() { python3 -m json.tool; }

echo "== 0. health =="
curl -fsS "$API/health" | j

echo "== 1. 建立批次 + 種入版本/spans (§5) =="
curl -fsS -X DELETE "$API/api/batches/$B" >/dev/null 2>&1 || true
curl -fsS -X POST "$API/api/batches" -H 'content-type: application/json' -d @- <<'JSON' | j
{
  "batch_id": "evt_smoke_geology_01",
  "domain": "geology",
  "domain_prior": "本場為海岸地質演講，常見豆腐岩、海蝕平台…",
  "versions": [
    {"vid":"v0","from":"asr_raw","text":"沿海可見豆腐巖地形","frozen":true}
  ],
  "spans": [
    {"span_id":"s_017","context":"沿海可見[豆腐巖]地形","is_proper_noun":true,
     "history":[
       {"iter":1,"value":"豆腐巖","by":"model"},
       {"iter":2,"value":"豆腐岩","by":"model"},
       {"iter":3,"value":"豆腐岩","by":"model"},
       {"iter":4,"value":"豆腐岩","by":"model"}]},
    {"span_id":"s_018","context":"沿海可見豆腐岩[地形]","is_proper_noun":false,
     "history":[
       {"iter":1,"value":"地形","by":"model"},
       {"iter":2,"value":"地形","by":"model"},
       {"iter":3,"value":"地形","by":"model"}]},
    {"span_id":"s_019","context":"位於[宏雞]一帶","is_proper_noun":true,
     "history":[
       {"iter":1,"value":"宏雞","by":"model"},
       {"iter":2,"value":"洪基","by":"model"},
       {"iter":3,"value":"宏基","by":"model"},
       {"iter":4,"value":"宏雞","by":"model"},
       {"iter":5,"value":"洪基","by":"model"},
       {"iter":6,"value":"宏基","by":"model"}]}
  ]
}
JSON

echo "== 2. 收斂一輪 (§3) — 期望 s_017=stable(pending待接地), s_018=auto(常用詞直過), s_019=volatile→human =="
curl -fsS -X POST "$API/api/batches/$B/converge" | j

echo "== 3. 對穩定專名 s_017 接地 (§3 RRF) — 期望 pass + decision=auto =="
curl -fsS -X POST "$API/api/batches/$B/spans/s_017/ground" \
  -H 'content-type: application/json' \
  -d '{"candidates":["豆腐岩","海蝕平台","豆腐礁"]}' | j

echo "== 4. 併入字典 (§9) =="
curl -fsS -X POST "$API/api/batches/$B/commit" | j

echo "== 5. 查字典 + sync 重生 ②regex =="
curl -fsS "$API/api/dictionary/terms" | j
curl -fsS -X POST "$API/api/dictionary/sync" | j

echo "✓ 煙霧測試結束"
