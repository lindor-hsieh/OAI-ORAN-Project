# =============================================================================
# Dockerfile — Local xApp Python 推論伺服器
#
# 5 個節點共用同一個 image，以 CMD 區分入口腳本。
# 演進路徑：Phase 3 啟發式 → Phase 4 DRL → Phase 5 Flower Client
# =============================================================================

FROM python:3.11-slim

WORKDIR /app

# 安裝系統相依（ZMQ runtime）
RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends libzmq5 && \
    rm -rf /var/lib/apt/lists/*

# 優先複製 requirements 以利用 Docker layer cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製推論伺服器程式碼
COPY inference_server.py .
COPY main.py .

# 預設入口（透過環境變數 NODE_ID 指定節點）
CMD ["python3", "main.py"]
