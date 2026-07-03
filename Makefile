# 收斂式字典 — 封閉建構操作入口
.DEFAULT_GOAL := help
SHELL := /bin/bash

API ?= http://localhost:8080

help:                ## 顯示說明
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n",$$1,$$2}'

env:                 ## 從範本建立 .env（若不存在）
	@test -f .env || cp .env.example .env && echo "已備妥 .env"

build: env           ## 建構 api 映像（vLLM/TEI 用既有映像）
	docker compose build api

up: env               ## 啟動整個封閉堆疊（模型全離線）
	docker compose up -d

ps:                  ## 容器狀態
	docker compose ps

logs:                ## 跟看日誌（make logs S=vllm）
	docker compose logs -f $(S)

down:                ## 停止
	docker compose down

health:              ## 查 API + 三個模型服務健康
	@curl -fsS $(API)/health | python3 -m json.tool

smoke:               ## 跑離線端到端煙霧測試
	@bash scripts/smoke_test.sh

pull-models:         ## 下載缺少的模型到 $MODELS_ROOT（STT/NER/g2pW…）
	@bash scripts/download_models.sh

verify-offline:      ## 確認 runtime 不依賴外網（檢查 OFFLINE 旗標）
	@docker compose config | grep -E "HF_HUB_OFFLINE|TRANSFORMERS_OFFLINE" && echo "OK: 模型服務皆設離線"

.PHONY: help env build up ps logs down health smoke pull-models verify-offline
