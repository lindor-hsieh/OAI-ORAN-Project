"""
training_pipeline.py — 共用的 DRL 訓練流程（MongoDB 讀取 → Actor-Critic 離線訓練 → 評估）

被兩個呼叫端共用，避免訓練邏輯維護兩份：
  - InferenceServer._run_training_round()（Non-RT 背景執行緒，每 60 秒，In-process，
    與近即時推論共用同一個 DRLAgent 記憶體實例）
  - flower-app/iab_fl/client_app.py 的 @app.train()／@app.evaluate()（FL 輪次，約每小時
    一次，由 flower-supernode 以獨立 subprocess 執行，透過磁碟 checkpoint 而非記憶體與
    InferenceServer 同步）

不負責：MongoDB 寫入緩衝區 flush、資料新鮮度判斷（staleness guard）、模型存檔——
這些跟呼叫端各自的執行環境（常駐迴圈 vs 一次性 subprocess）綁定，留給呼叫端處理。

2026-09-18：依 drl_agent.py 的 MODEL_ARCH 開關分派兩種資料抓取方式：
  - MODEL_ARCH=mlp（Stage 2~4 預設）：fetch_experiences()，打散抽樣獨立經驗，
    不要求時間連續性，門檻只看 MIN_TRAIN_EXPERIENCES 原始經驗數。
  - MODEL_ARCH=gru：fetch_sequences()（2026-07-09 為配合 GRU 架構導入，GRU
    需要序列內部真的有時間上的先後關係，才有記憶可學），額外要求切出足夠的
    時間連續序列。
run_training_round() 依 agent.arch 決定呼叫哪一個、餵給 agent.train_on_batch()
的資料形狀也跟著不同（agent.train_on_batch() 內部依 agent.arch 再分派一次，
見 drl_agent.py）。
"""

from __future__ import annotations

import contextlib
import logging
import threading
from typing import Optional

import numpy as np
import pymongo

from drl_agent import DRLAgent, MIN_TRAIN_EXPERIENCES, TRAIN_BATCH_SIZE, TRAIN_SEQ_LEN, TRAIN_SEQ_COUNT

TRAIN_FETCH_LIMIT: int = 2000
TRAIN_EPOCHS_PER_ROUND: int = 10

# jfi_raw／r_throughput：Lagrangian 乘子 λ 更新用（DRLAgent.train_on_batch()，
# r_throughput 用來排除閒置樣本不計入 JFI 平均，見 drl_agent.py 說明）
_PROJECTION = {
    "state_vec": 1, "mask_vec": 1, "action_ratios": 1,
    "reward": 1, "next_state_vec": 1, "next_mask_vec": 1,
    "jfi_raw": 1, "r_throughput": 1, "is_idle": 1,
    "_id": 0,
}


def _is_contiguous(doc_a: dict, doc_b: dict) -> bool:
    """
    判斷兩筆經驗是不是時間上緊接著的連續轉換。

    對於真正連續（沒有中斷）的兩筆經驗，doc_a 的 next_state_vec 跟 doc_b 的
    state_vec 是同一次 encode_state() 呼叫算出來的（見 inference_server.py
    的 _build_rl_experience()／主迴圈快取邏輯），逐位元組相同。若中間發生
    跳過寫入（例如舊版的閒置轉換過濾，或 ZMQ 逾時），這個相等關係就會斷掉。
    這比額外加一個序號欄位更精確，且不需要新的 MongoDB schema 欄位。
    """
    return doc_a.get("next_state_vec") == doc_b.get("state_vec")


def fetch_experiences(
    mongo_col: pymongo.collection.Collection,
    fetch_limit: int = TRAIN_FETCH_LIMIT,
    log: Optional[logging.Logger] = None,
) -> list[dict]:
    """
    從 MongoDB 讀取最近 fetch_limit 筆經驗（打散抽樣用，MODEL_ARCH=mlp 專用）。

    跟 fetch_sequences() 不同，這裡不要求時間連續性、不切窗——MLP 是無記憶的
    單步模型，訓練時每筆經驗獨立看待即可，門檻只看原始經驗數
    （MIN_TRAIN_EXPERIENCES，run_training_round() 判斷）。

    Returns:
        experiences : 原始經驗 list[dict]（按時間升序）
    """
    try:
        cursor = (
            mongo_col
            .find(
                {"reward": {"$exists": True}, "next_state_vec": {"$exists": True}},
                projection=_PROJECTION,
            )
            .sort("timestamp", pymongo.ASCENDING)
            .limit(fetch_limit)
        )
        experiences = list(cursor)
    except pymongo.errors.PyMongoError as exc:
        if log:
            log.warning("讀取訓練資料失敗: %s", exc)
        return []

    if log:
        log.info("讀取到 %d 筆原始經驗（打散抽樣，MLP）", len(experiences))
    return experiences


def fetch_sequences(
    mongo_col: pymongo.collection.Collection,
    fetch_limit: int = TRAIN_FETCH_LIMIT,
    seq_len: int = TRAIN_SEQ_LEN,
    log: Optional[logging.Logger] = None,
) -> tuple[list[list[dict]], int]:
    """
    MODEL_ARCH=gru 專用。從 MongoDB 讀取最近 fetch_limit 筆經驗（按時間升序），
    切成一段一段時間上連續的「運行」（run），每段運行再切成長度 seq_len、
    彼此不重疊的定長序列。

    不重疊視窗（stride=seq_len）是刻意的：訓練時要以「整個序列」為單位做
    train/test 切分避免洩漏——重疊視窗會共享大部分時間步，會讓切分後的
    overfitting 偵測失真。

    Returns:
        (sequences, n_raw)：sequences 是候選序列池；n_raw 是實際抓到的原始
        經驗筆數，供呼叫端判斷資料是否足夠累積（跟序列數是不同的判斷維度：
        原始經驗夠多、但如果大多是零散的短暫連續片段，序列數可能還是不夠）。
    """
    try:
        cursor = (
            mongo_col
            .find(
                {"reward": {"$exists": True}, "next_state_vec": {"$exists": True}},
                projection=_PROJECTION,
            )
            .sort("timestamp", pymongo.ASCENDING)
            .limit(fetch_limit)
        )
        experiences = list(cursor)
    except pymongo.errors.PyMongoError as exc:
        if log:
            log.warning("讀取訓練資料失敗: %s", exc)
        return [], 0

    n_raw = len(experiences)
    if n_raw == 0:
        return [], 0

    # 切成連續運行
    runs: list[list[dict]] = [[experiences[0]]]
    for prev, curr in zip(experiences, experiences[1:]):
        if _is_contiguous(prev, curr):
            runs[-1].append(curr)
        else:
            runs.append([curr])

    # 每段運行切成不重疊的定長序列
    sequences: list[list[dict]] = []
    for run in runs:
        for start in range(0, len(run) - seq_len + 1, seq_len):
            sequences.append(run[start:start + seq_len])

    if log:
        log.info(
            "讀取到 %d 筆原始經驗，切成 %d 段連續運行，%d 個長度 %d 的訓練序列",
            n_raw, len(runs), len(sequences), seq_len,
        )
    return sequences, n_raw


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
    依 agent.arch 分派：MODEL_ARCH=mlp 呼叫 _run_training_round_mlp()（打散
    抽樣、無時間連續性要求）；MODEL_ARCH=gru 呼叫 _run_training_round_gru()
    （時間連續序列）。兩者對外回傳 metrics dict 的核心欄位一致
    （train_step/actor_loss/critic_loss/entropy/mean_reward/mean_adv/lambda/
    batch_jfi_mean/test_* 等），只有訓練樣本計數欄位不同（mlp: n_train_exp/
    n_test_exp；gru: n_train_seq/n_test_seq/seq_len）。若資料不足或讀取失敗
    則回傳 {}。**不呼叫 agent.save()**——由呼叫端決定何時、用什麼路徑存檔。

    `lock`（選填）：只包住實際觸碰 agent 權重的梯度更新／評估段落，MongoDB
    讀取與 train/test split 都在鎖外執行，見兩個實作各自的說明。
    """
    if agent.arch == "gru":
        return _run_training_round_gru(agent, mongo_col, epochs, fetch_limit, min_experiences, log, lock)
    return _run_training_round_mlp(agent, mongo_col, epochs, fetch_limit, min_experiences, log, lock)


def _run_training_round_mlp(
    agent: DRLAgent,
    mongo_col: pymongo.collection.Collection,
    epochs: int,
    fetch_limit: int,
    min_experiences: int,
    log: Optional[logging.Logger],
    lock: Optional[threading.Lock],
) -> dict:
    """
    讀取打散的獨立經驗（fetch_experiences()）、8:2 切 train/test（以單筆經驗
    為單位，i.i.d.，無時間步洩漏疑慮）、訓練 epochs 輪、於測試集評估。

    門檻只看 MIN_TRAIN_EXPERIENCES 原始經驗數，不像 GRU 分支還要額外滿足
    時間連續序列數——這是 MODEL_ARCH=mlp 訓練比 GRU 容易觸發的主因。
    """
    experiences = fetch_experiences(mongo_col, fetch_limit=fetch_limit, log=log)
    n_raw = len(experiences)

    if n_raw < min_experiences:
        if log:
            log.info("經驗數量不足 (需 %d 筆)，等待更多資料累積...", min_experiences)
        return {}

    n = len(experiences)
    test_size = min(max(1, int(n * 0.2)), max(0, n - TRAIN_BATCH_SIZE))
    test_idxs = set(np.random.choice(n, test_size, replace=False).tolist()) if test_size > 0 else set()
    train_exp = [e for i, e in enumerate(experiences) if i not in test_idxs]
    test_exp = [e for i, e in enumerate(experiences) if i in test_idxs]

    last_metrics: dict = {}
    for _ in range(epochs):
        ctx = lock if lock is not None else contextlib.nullcontext()
        with ctx:
            m = agent.train_on_batch(train_exp)
        if m:
            last_metrics = m

    if not last_metrics:
        return {}

    ctx = lock if lock is not None else contextlib.nullcontext()
    with ctx:
        test_metrics = agent.evaluate_on_batch(test_exp)

    result: dict = dict(last_metrics)
    result.update(test_metrics)
    result["n_train_exp"] = len(train_exp)
    result["n_test_exp"] = len(test_exp)
    return result


def _run_training_round_gru(
    agent: DRLAgent,
    mongo_col: pymongo.collection.Collection,
    epochs: int,
    fetch_limit: int,
    min_experiences: int,
    log: Optional[logging.Logger],
    lock: Optional[threading.Lock],
) -> dict:
    """
    讀取近期時間連續的經驗序列（fetch_sequences()）、8:2 切 train/test（以
    序列為單位，避免時間步層級的洩漏）、訓練 epochs 輪、於測試集評估。

    `lock`：**鎖以每個 epoch 為單位個別取得/釋放**（2026-07-09 GRU 改版後的
    修正，不是整個 epochs 迴圈包一個鎖）：GRU 的 BPTT 反向傳播比 MLP 重得多，
    若整段訓練迴圈共用一個鎖，會讓等待中的近即時 infer() 卡到數秒（實測
    3~4 秒），遠超 C xApp 的 5ms REQ 逾時。拆成逐 epoch 鎖，讓 infer() 有
    機會在 epoch 邊界插隊，總訓練時間不變，但不再是一整段連續的 DRL 空窗。
    """
    sequences, n_raw = fetch_sequences(mongo_col, fetch_limit=fetch_limit, log=log)

    if n_raw < min_experiences:
        if log:
            log.info("經驗數量不足 (需 %d 筆)，等待更多資料累積...", min_experiences)
        return {}

    if len(sequences) < TRAIN_SEQ_COUNT:
        if log:
            log.info(
                "候選序列數量不足 (有 %d 個，需 %d 個)，等待更多時間連續的資料累積...",
                len(sequences), TRAIN_SEQ_COUNT,
            )
        return {}

    # ── Train / Test split（8:2，以整個序列為單位）─────────────────────
    # 測試集用於偵測 overfitting：呼叫端可比較 test_actor_loss 與 actor_loss。
    # test_size 上限確保切完之後 train 至少留下 TRAIN_SEQ_COUNT 個（否則
    # train_on_batch() 會直接跳過整輪訓練），測試集品質是次要考量——樣本
    # 不足時 evaluate_on_batch() 會自行優雅回傳 {}，不會出錯。
    n_seq = len(sequences)
    test_size = min(max(1, int(n_seq * 0.2)), max(0, n_seq - TRAIN_SEQ_COUNT))
    test_idxs = set(np.random.choice(n_seq, test_size, replace=False).tolist()) if test_size > 0 else set()
    train_seq = [s for i, s in enumerate(sequences) if i not in test_idxs]
    test_seq = [s for i, s in enumerate(sequences) if i in test_idxs]

    last_metrics: dict = {}
    for _ in range(epochs):
        ctx = lock if lock is not None else contextlib.nullcontext()
        with ctx:
            m = agent.train_on_batch(train_seq)
        if m:
            last_metrics = m

    if not last_metrics:
        return {}

    ctx = lock if lock is not None else contextlib.nullcontext()
    with ctx:
        test_metrics = agent.evaluate_on_batch(test_seq)

    result: dict = dict(last_metrics)
    result.update(test_metrics)
    result["n_train_seq"] = len(train_seq)
    result["n_test_seq"] = len(test_seq)
    return result
