# 收斂字典 API — 以本地已存在的 python:3.12-slim 為底，封閉 runtime。
FROM python:3.12-slim

WORKDIR /app

# 系統相依：curl 供健檢；espeak-ng＝非漢字語系的規則式 G2P（grapheme→IPA，天條1、runtime 保留、離線）；
# g++ 供 hnswlib 從原始碼編譯（PyPI 無預編譯 wheel），編完後 purge g++ 保持 runtime 精簡。
COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl espeak-ng g++ \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove g++ \
    && rm -rf /var/lib/apt/lists/*

COPY app/ ./app/

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=5 --start-period=15s \
    CMD curl -fsS http://localhost:8080/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
