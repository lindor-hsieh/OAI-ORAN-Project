# =============================================================================
# Dockerfile — Local xApp Python 推論伺服器 (Phase 4 DRL 版本)
#
# 5 個節點共用同一個 image，以環境變數 NODE_ID 區分入口。
# 演進路徑：Phase 3 啟發式 → Phase 4 DRL Actor-Critic → Phase 5 Flower Client
# =============================================================================

FROM python:3.11-slim

WORKDIR /app

# 安裝系統相依（ZMQ runtime）
RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends libzmq5 && \
    rm -rf /var/lib/apt/lists/*

# 優先複製 requirements，利用 Docker layer cache
COPY requirements.txt .

# 安裝 CPU-only PyTorch（避免拉取 CUDA 版本，縮短 build 時間）
# 先安裝 torch，再安裝其他套件
RUN pip install --no-cache-dir \
    torch --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt

# 複製推論伺服器程式碼
COPY drl_agent.py .
COPY reward_calculator.py .
COPY inference_server.py .
COPY main.py .

# 建立模型儲存目錄（可透過 volume 掛載以持久化）
RUN mkdir -p /app/models

# 預設入口（透過環境變數 NODE_ID 指定節點）
CMD ["python3", "main.py"]
