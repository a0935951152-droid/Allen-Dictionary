# NER 封閉服務（§3 便宜濾網「本地 Chinese NER」）：ckiplab/bert-base-chinese-ner，CPU 推論。
# 建構期(host 網路)裝 torch CPU + transformers；runtime 全離線（HF_HUB_OFFLINE）。
FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir torch==2.5.1 --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "80"]
HEALTHCHECK --interval=30s --timeout=10s --retries=20 --start-period=180s \
  CMD curl -fsS http://localhost:80/health || exit 1
