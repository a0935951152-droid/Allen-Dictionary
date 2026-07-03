#!/usr/bin/env python3
"""站台儀表板全流程 e2e（離線）：建批→ingest→segment→分類→review(r1)→converge→ground→commit→sync→rollback。
用法：python3 scripts/e2e_dashboard.py（需先 `dic up`，會等 breeze 就緒）。"""
import json, time, urllib.request, urllib.error
BASE = "http://localhost:8080"

def req(method, path, body=None, max_time=120):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=max_time) as resp:
            t = resp.read().decode()
            return resp.status, (json.loads(t) if t else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

# 1) 等 breeze(llm) 就緒
h = None
for i in range(72):
    _, h = req("GET", "/health")
    if h and h.get("services", {}).get("llm"):
        break
    time.sleep(5)
print("llm ready:", h)

bid = "test_flow"
req("DELETE", "/api/batches/" + bid)
print("create:", req("POST", "/api/batches", {"batch_id": bid, "domain": "geology"})[0])

RAW = ("各位好，今天我們在野柳一帶觀察海岸地質。沿海可以看到豆腐巖這種地形，"
       "它是砂岩受到節理切割後形成的。旁邊的海蝕平台也很明顯。"
       "等一下走到宏雞那一帶，那裡的岩體更完整。")
print("ingest:", req("POST", f"/api/batches/{bid}/ingest", {"text": RAW, "filename": "talk.txt"}))
print("segment:", req("POST", f"/api/batches/{bid}/segment?max_chars=60")[1])
print("classify:", req("POST", "/api/classifications",
      {"name": "地質", "prompt": "判斷詞是否為地形/岩體/海岸地名等地質專名；標出疑似 ASR 錯字並給標準寫法。"})[0])

print("review:", req("POST", f"/api/batches/{bid}/review", {"classification": "地質"}, max_time=400)[1])
_, r1 = req("GET", f"/api/batches/{bid}/r1")
print("r1 rows:", len(r1["rows"]), "spans:", sum(len(x["spans"]) for x in r1["rows"]), "classification:", r1["classification"])

print("converge:", req("POST", f"/api/batches/{bid}/converge", {"classification": "地質"})[1])
_, tree = req("GET", f"/api/batches/{bid}/tree")
print("tree segs:", len(tree["segments"]), "seg ranks:", [s["rank"] for s in tree["segments"]])

_, spans = req("GET", f"/api/batches/{bid}/spans")
print("spans:", [(x["span_id"], x["history"][-1]["value"], x.get("review"), x.get("category"), x.get("is_proper_noun"), x["rank"]) for x in spans])

if spans:
    sid = spans[0]["span_id"]
    _, g = req("POST", f"/api/batches/{bid}/spans/{sid}/ground", {"candidates": ["豆腐岩", "豆腐礁", "海蝕平台"]})
    print("ground", sid, ":", g.get("grounding"), "decision:", g.get("decision"))

print("converge2:", req("POST", f"/api/batches/{bid}/converge")[1].get("ran"))
print("commit:", req("POST", f"/api/batches/{bid}/commit")[1])
print("sync:", req("POST", "/api/dictionary/sync")[1])
print("rollback:", req("POST", f"/api/batches/{bid}/rollback")[1])
