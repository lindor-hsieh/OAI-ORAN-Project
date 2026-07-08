# =============================================================================
# Dockerfile — Local xApp Python 推論伺服器 (Phase 5 Flower ServerApp/ClientApp 版本)
#
# 5 個節點共用同一個 image，以環境變數 NODE_ID 區分入口；同一份 image 也用來跑
# Phase 5 的 flower-superlink / flower-supernode-nodeN / flower-scheduler 服務，
# 由 docker-compose 的 command: 覆蓋不同入口，不需要另建 image。
# 演進路徑：Phase 3 啟發式 → Phase 4 DRL Actor-Critic → Phase 5 Flower ServerApp/ClientApp
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

# 安裝 vendor 進來的 flwr（本機 editable install，非 PyPI；見 vendor/pyproject.toml。
# 之後若要自訂 JFI-guided aggregation，直接改 vendor/flwr/ 底下的框架原始碼即可）
COPY vendor/ vendor/
RUN pip install --no-cache-dir -e ./vendor

# 複製推論伺服器程式碼
COPY drl_agent.py .
COPY reward_calculator.py .
COPY training_pipeline.py .
COPY inference_server.py .
COPY main.py .

# Phase 5 Global xApp 回傳配額橋接（見 global_xapp_bridge.py 檔頭說明）
COPY global_xapp.py .
COPY global_xapp_bridge.py .

# 複製 Phase 5 Flower App 套件（ServerApp/ClientApp + 排程 wrapper）
# 目錄名不能有底線（flwr build/run 會驗證 FAB 根目錄名稱，只允許字母/數字/連字號）
COPY flower-app/ flower-app/
RUN chmod +x flower-app/run_hourly.sh

# Flower CLI 全域設定（SuperLink 連線位址）烤進 image，避免每個一次性容器
# 都要重新觸發脆弱的 pyproject.toml 自動遷移機制（見 flwr_config.toml 說明）
RUN mkdir -p /root/.flwr
COPY flwr_config.toml /root/.flwr/config.toml

# 建立模型儲存目錄（可透過 volume 掛載以持久化）
RUN mkdir -p /app/models

# 預設入口（透過環境變數 NODE_ID 指定節點）；flower-superlink/-supernode/-scheduler
# 服務由 docker-compose 的 command: 覆蓋成各自的入口
CMD ["python3", "main.py"]
