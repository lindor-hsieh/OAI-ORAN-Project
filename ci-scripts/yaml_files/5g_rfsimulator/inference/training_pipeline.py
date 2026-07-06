"""
training_pipeline.py — 共用的 DRL 訓練流程（MongoDB 讀取 → Actor-Critic 離線訓練 → 評估）

被兩個呼叫端共用，避免訓練邏輯維護兩份：
  - InferenceServer._run_training_round()（Non-RT 背景執行緒，每 60 秒，In-process，
    與近即時推論共用同一個 DRLAgent 記憶體實例）
  - flower-app/iab_fl/client_app.py 的 @app.train()（FL 輪次，約每小時一次，由 flower-supernode
    以獨立 subprocess 執行，透過磁碟 checkpoint 而非記憶體與 InferenceServer 同步）

不負責：MongoDB 寫入緩衝區 flush、資料新鮮度判斷（staleness guard）、模型存檔——
這些跟呼叫端各自的執行環境（常駐迴圈 vs 一次性 subprocess）綁定，留給呼叫端處理。
"""

from __future__ import annotations

import contextlib
import logging
import threading
from typing import Optional

import numpy as np
import pymongo

from drl_agent import DRLAgent, MIN_TRAIN_EXPERIENCES, TRAIN_BATCH_SIZE

TRAIN_FETCH_LIMIT: int = 2000
TRAIN_EPOCHS_PER_ROUND: int = 10


def run_training_round(
    agent: DRLAgent,
    mongo_col: pymongo.collection.Collection,
    epochs: int = TRAIN_EPOCHS_PER_ROUND,
    fetch_limit: int = TRAIN_FETCH_LIMIT,
    min_experiences: int = MIN_TRAIN_EXPERIENCES,
    log: Optional[logging.Logger] = None,
    lock: Optional[threading.Lock] = None,
) -> dict:
    """
    讀取近期 (s, a, r, s') 經驗、8:2 切 train/test、訓練 epochs 輪、於測試集評估。

    回傳合併後的 metrics dict（train_step/actor_loss/critic_loss/entropy/
    mean_reward/mean_adv 等訓練指標 + test_actor_loss/test_critic_loss/
    test_entropy/test_mean_reward 等測試指標 + n_train/n_test 樣本數）。
    若資料不足或讀取失敗則回傳 {}。**不呼叫 agent.save()**——由呼叫端決定
    何時、用什麼路徑存檔（in-process 常駐迴圈 vs FL ClientApp 存檔時機不同）。

    `lock`（選填）：只包住實際觸碰 agent 權重的梯度更新／評估段落，
    MongoDB 讀取與 train/test split 都在鎖外執行，避免長時間佔用鎖。
    呼叫端若在單一 process 內與其他執行緒共用同一個 agent（例如
    InferenceServer 的近即時 ZMQ 迴圈），應傳入該 process 的模型鎖；
    若 agent 在本次呼叫的 process 中是唯一擁有者（例如 FL ClientApp
    的獨立 subprocess），可省略此參數。
    """
    try:
        cursor = (
            mongo_col
            .find(
                {"reward": {"$exists": True}, "next_state_vec": {"$exists": True}},
                projection={
                    "state_vec": 1, "mask_vec": 1, "action_ratios": 1,
                    "reward": 1, "next_state_vec": 1, "next_mask_vec": 1,
                    "_id": 0,
                },
            )
            .sort("timestamp", pymongo.DESCENDING)
            .limit(fetch_limit)
        )
        experiences = list(cursor)
    except pymongo.errors.PyMongoError as exc:
        if log:
            log.warning("讀取訓練資料失敗: %s", exc)
        return {}

    n = len(experiences)
    if log:
        log.info("讀取到 %d 筆 RL 經驗，準備訓練", n)

    if n < min_experiences:
        if log:
            log.info("經驗數量不足 (需 %d 筆)，等待更多資料累積...", min_experiences)
        return {}

    # ── Train / Test split（8:2）────────────────────────────────────────
    # 測試集用於偵測 overfitting：呼叫端可比較 test_actor_loss 與 actor_loss。
    test_size = max(TRAIN_BATCH_SIZE, int(n * 0.2))
    test_idxs = set(np.random.choice(n, test_size, replace=False).tolist())
    train_exp = [e for i, e in enumerate(experiences) if i not in test_idxs]
    test_exp = [e for i, e in enumerate(experiences) if i in test_idxs]

    ctx = lock if lock is not None else contextlib.nullcontext()
    last_metrics: dict = {}
    with ctx:
        for _ in range(epochs):
            m = agent.train_on_batch(train_exp)
            if m:
                last_metrics = m

        if not last_metrics:
            return {}

        test_metrics = agent.evaluate_on_batch(test_exp)

    result: dict = dict(last_metrics)
    result.update(test_metrics)
    result["n_train"] = len(train_exp)
    result["n_test"] = len(test_exp)
    return result
