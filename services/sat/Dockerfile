# SaT 句分割封閉服務（segment-any-text/sat-3l-sm，字節級多語、不靠標點、CPU 推論）。
# 建構期(host 網路)裝 torch CPU + wtpsplit；runtime 全離線（HF_HUB_OFFLINE），模型從 /models 掛載本地載入。
FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir torch==2.5.1 --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1
EXPOSE 80
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "80"]
HEALTHCHECK --interval=30s --timeout=10s --retries=40 --start-period=300s \
  CMD curl -fsS http://localhost:80/health || exit 1
