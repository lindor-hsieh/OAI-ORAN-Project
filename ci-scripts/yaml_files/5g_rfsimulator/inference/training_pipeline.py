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
import random
import os
import threading
from typing import Optional

import numpy as np
import pymongo

import bisect
import math

from drl_agent import (DRLAgent, MAX_BSR, MAX_UE_COUNT, MIN_TRAIN_EXPERIENCES, TRAIN_BATCH_SIZE, TRAIN_SEQ_COUNT,
                       TRAIN_SEQ_LEN, UE_FEAT_DIM, PF_TIER as PF_TIER_IDX)
from reward_calculator import REWARD_MODE

# ── v3 reward：子樹 α-fair 效用（2026-10-03，REWARD_MODE=alpha_fair）──────────────────────────────
# r_t = Σ_{u ∈ 本節點子樹的終端 UE} U_α(x_u)，x_u＝該 UE 在這個 1 秒視窗的下行吞吐量（模擬 Mbps）。
# relay 的子樹＝自己的直連 UE＋下游兩個 access 節點的 UE（不含 MT，MT 的流量就是下游 UE 的流量）；access＝自己的 UE。
# U_α(x)=x^(1−α)/(1−α)（α=1 時 log(x+ε)）：α=0 純吞吐量、α=1 比例公平（≈PF 的目標），文獻 Mo & Walrand 2000。
# 平台實測（HISTORY 續五十九／六十）：α=0 使最佳動作恆為「全力遮」、α=1 恆為「不遮」，α=0.5 隨狀態改變。
# 下游 UE 的吞吐量從子節點的經驗文件依時間戳對齊（±ALPHA_JOIN_TOL_S）；對不到的經驗丟棄（不用缺項的 reward）。
# 訓練讀取時重算（線上寫入的 reward 只是佔位），本地訓練與 FL 客戶端都經 fetch_experiences()，定義一致。
ALPHA_FAIR: float = float(os.getenv("DRL_ALPHA", "0.5"))
ALPHA_EPS: float = 0.1
SIM_SPEED: float = float(os.getenv("RFSIM_SPEED", "0.3"))     # 牆鐘→模擬時間：模擬 Mbps = 牆鐘 Mbps ÷ S
ALPHA_JOIN_TOL_S: float = 1.5
RELAY_CHILDREN: dict[int, tuple[int, int]] = {1: (5, 6), 2: (7, 8), 3: (9, 10), 4: (11, 12)}


def alpha_utility(x: float, alpha: float = ALPHA_FAIR) -> float:
    if abs(alpha - 1.0) < 1e-9:
        return math.log(x + ALPHA_EPS)
    return max(x, 0.0) ** (1.0 - alpha) / (1.0 - alpha)


def end_ue_tput(next_state_vec: list, next_mask_vec: list) -> list[float]:
    """由 S_t 的編碼還原活躍終端 UE（非 MT）的吞吐量（模擬 Mbps）：state 第 UE_FEAT_DIM·i 維＝log1p(bsr)/log1p(MAX_BSR)。"""
    out = []
    lmax = math.log1p(MAX_BSR)
    for i in range(min(len(next_mask_vec), MAX_UE_COUNT)):
        if not next_mask_vec[i] or next_state_vec[i * UE_FEAT_DIM + 3] > 0.5:
            continue
        bsr = math.expm1(next_state_vec[i * UE_FEAT_DIM] * lmax)
        out.append(bsr * 8.0 / 1e6 / SIM_SPEED)
    return out


def apply_alpha_fair_reward(mongo_col: pymongo.collection.Collection, experiences: list[dict],
                            log: Optional[logging.Logger] = None) -> list[dict]:
    """把 experiences 的 reward 改成子樹 α-fair 效用；relay 的經驗對不到子節點文件者丟棄。回傳保留的經驗。"""
    if not experiences:
        return experiences
    try:
        node = int(mongo_col.name.split("_")[0][4:])
    except ValueError:
        return experiences
    children = RELAY_CHILDREN.get(node, ())
    child_idx: list[tuple[list, list]] = []
    if children:
        ts = [e["timestamp"] for e in experiences if e.get("timestamp") is not None]
        if not ts:
            return []
        import datetime as _dt
        lo, hi = min(ts) - _dt.timedelta(seconds=5), max(ts) + _dt.timedelta(seconds=5)
        db = mongo_col.database
        for c in children:
            docs = list(db[f"node{c}_experiences"].find(
                {"timestamp": {"$gte": lo, "$lte": hi}, "next_state_vec": {"$exists": True}},
                {"_id": 0, "timestamp": 1, "next_state_vec": 1, "next_mask_vec": 1}).sort("timestamp", 1))
            child_idx.append(([d["timestamp"].timestamp() for d in docs], docs))
    kept, dropped = [], 0
    for e in experiences:
        xs = end_ue_tput(e["next_state_vec"], e["next_mask_vec"])
        ok = True
        if children:
            t = e.get("timestamp")
            if t is None:
                ok = False
            else:
                t = t.timestamp()
                for tl, docs in child_idx:
                    k = bisect.bisect_left(tl, t)
                    best = min((j for j in (k - 1, k) if 0 <= j < len(tl)), key=lambda j: abs(tl[j] - t), default=None)
                    if best is None or abs(tl[best] - t) > ALPHA_JOIN_TOL_S:
                        ok = False
                        break
                    xs += end_ue_tput(docs[best]["next_state_vec"], docs[best]["next_mask_vec"])
        if not ok:
            dropped += 1
            continue
        e["reward"] = float(sum(alpha_utility(x) for x in xs))
        kept.append(e)
    if log:
        log.info("α-fair reward（α=%.2f，子樹＝%s）：保留 %d 筆、對不到子節點丟棄 %d 筆",
                 ALPHA_FAIR, f"自己＋node{children}" if children else "自己", len(kept), dropped)
    return kept

# 回放緩衝區大小：每輪訓練讀取「最新」的這麼多筆經驗。每節點 ~1 筆/秒（控制週期 1 秒），5000 筆 ≈ 83 分鐘 ≈ 45 個
# 110 秒相位，足以涵蓋多輪正常/壅塞交替（舊註解的「~10 筆/秒」是錯的）。2026-09-26 修正：舊版是 2000 筆且取的是「最舊的」2000 筆
# （見 fetch_experiences()），資料一超過 2000 筆就永遠在重複訓練最早那批啟發式階段的資料。
# 可用環境變數 TRAIN_FETCH_LIMIT 覆寫（inference_server.py 與 FL client_app.py 共用這個值）。
TRAIN_FETCH_LIMIT: int = int(os.getenv("TRAIN_FETCH_LIMIT", "5000"))
TRAIN_EPOCHS_PER_ROUND: int = 10

# jfi_raw／r_throughput：Lagrangian 乘子 λ 更新用（DRLAgent.train_on_batch()，
# r_throughput 用來排除閒置樣本不計入 JFI 平均，見 drl_agent.py 說明）
_PROJECTION = {
    "state_vec": 1, "mask_vec": 1, "action_ratios": 1,
    "action_tiers": 1,    # Local DRL v2（MLP）的每 UE 檔位動作；action_ratios 只剩 GRU 舊分支用
    "reward": 1, "next_state_vec": 1, "next_mask_vec": 1,
    "jfi_raw": 1, "r_throughput": 1, "is_idle": 1,
    "behavior_logp": 1,   # PPO 比例裁剪用的行為策略 log π(a|s)，見 drl_agent.py（缺欄位者只訓練 Critic）
    "macro_action": 1,    # Local DRL v2.1 宏動作（2026-10-02；漏讀會讓 _filter_valid_experiences 把全部經驗濾掉）
    "timestamp": 1,       # v3 α-fair reward 依時間戳對齊子節點
    "factored_action": 1, # v3.1 兩段式動作（漏讀會讓 _filter_valid_experiences 把全部經驗濾掉）
    "hold_id": 1, "hold_pos": 1,   # v3.3 動作持續（merge_action_holds）
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
    從 MongoDB 讀取「最新」的 fetch_limit 筆經驗（打散抽樣用，MODEL_ARCH=mlp 專用）。

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
            # 必須 DESCENDING 才是「最新」的 N 筆；舊版 ASCENDING+limit 取到的是最早的 N 筆，
            # 集合超過 fetch_limit 之後永遠讀同一批舊資料（2026-09-26 修正）。
            .sort("timestamp", pymongo.DESCENDING)
            .limit(fetch_limit)
        )
        experiences = list(cursor)
        experiences.reverse()   # 還原成時間升序（MLP 不依賴順序，但維持函式對外承諾一致）
    except pymongo.errors.PyMongoError as exc:
        if log:
            log.warning("讀取訓練資料失敗: %s", exc)
        return []

    if log:
        log.info("讀取到 %d 筆原始經驗（最新 %d 筆內，打散抽樣，MLP）", len(experiences), fetch_limit)
    if REWARD_MODE == "alpha_fair":
        experiences = apply_alpha_fair_reward(mongo_col, experiences, log)
    online = attach_nstep_returns(merge_action_holds(experiences, log), log)
    offline = load_offline_decisions(mongo_col, log)
    if offline:
        # 混入比例固定（2026-10-04 Codex 審查：原本整批接上，離線約佔八成）：離線筆數＝max(線上×比例, 下限)
        k = min(len(offline), max(int(len(online) * OFFLINE_RATIO), OFFLINE_MIN))
        offline = random.sample(offline, k)
        if log:
            log.info("離線資料混入 %d 筆（線上 %d 筆，比例 %.2f、下限 %d）", k, len(online), OFFLINE_RATIO, OFFLINE_MIN)
    return [e for e in online + offline if _legal_action(e)]


# offline-to-online（2026-10-04）：線上微調時持續混入事先整理好的離線探索資料（每節點一個 pickle，放在模型目錄），
# 避免只用最近的線上資料把預訓練學到的東西洗掉。檔案不存在＝不混入。
OFFLINE_DATA_DIR: str = os.getenv("DRL_OFFLINE_DATA_DIR", os.getenv("MODEL_DIR", "/app/models"))
OFFLINE_RATIO: float = float(os.getenv("DRL_OFFLINE_RATIO", "1.0"))
OFFLINE_MIN: int = int(os.getenv("DRL_OFFLINE_MIN", "256"))


def _legal_action(e: dict) -> bool:
    """排除現行動作集不允許的動作（遮 MT；舊 PPO 資料裡有）。只對有 factored_action 的經驗檢查。"""
    fa = e.get("factored_action")
    if not fa or int(fa.get("tier", -1)) == PF_TIER_IDX:
        return True
    sv, ap = e.get("state_vec", []), fa.get("apply", [])
    for j, a in enumerate(ap[:MAX_UE_COUNT]):
        if a == 1 and j * UE_FEAT_DIM + 3 < len(sv) and sv[j * UE_FEAT_DIM + 3] > 0.5:
            return False
    return True
_OFFLINE_CACHE: dict = {}


def load_offline_decisions(mongo_col, log: Optional[logging.Logger] = None) -> list[dict]:
    try:
        node = int(mongo_col.name.split("_")[0][4:])
    except (ValueError, AttributeError):
        return []
    path = os.path.join(OFFLINE_DATA_DIR, f"offline_node{node}.pkl")
    if not os.path.exists(path):
        return []
    mt = os.path.getmtime(path)
    hit = _OFFLINE_CACHE.get(path)
    if hit is None or hit[0] != mt:
        import pickle
        with open(path, "rb") as f:
            hit = (mt, pickle.load(f))
        _OFFLINE_CACHE[path] = hit
        if log:
            log.info("離線探索資料：載入 %d 筆決策（%s）", len(hit[1]), path)
    return hit[1]


# v3.4 n 步回報（2026-10-04）：一步 TD 的自舉項 γV(s') 在 relay「該遮」狀態被 Critic 嚴重低估（遮罩讓 MT 佇列消化後，Critic 把
# 「佇列變短」當成「需求變低」，V(s') 比實際下降多約 3 倍），蓋掉遮罩的立即好處；改用前 n 個決策的實際 reward、只在第 n 步之後
# 才用 Critic 自舉（A3C／PPO 的 n-step return）。DRL_NSTEP=1＝一步 TD（v3.3 以前）。
NSTEP: int = max(1, int(os.getenv("DRL_NSTEP", "1")))
HOLD_K: int = max(1, int(os.getenv("DRL_ACTION_HOLD", "1")))
NSTEP_GAMMA: float = float(os.getenv("DRL_GAMMA_MLP", "0.5"))
NSTEP_MAX_GAP_S: float = float(os.getenv("DRL_NSTEP_MAX_GAP_S", str(max(9, int(os.getenv("DRL_ACTION_HOLD", "1")) + 4))))   # 相鄰決策間隔超過此值（中斷）就截斷


def _contiguous(prev: dict, nxt: dict) -> bool:
    """前一個決策的 s′ 是否就是下一個決策的 s（比對每子節點特徵；2026-10-04 Codex 審查：原本只看時間間隔）。"""
    a, b = prev.get("next_state_vec"), nxt.get("state_vec")
    if a is None or b is None or prev.get("next_mask_vec") != nxt.get("mask_vec"):
        return False
    n = MAX_UE_COUNT * UE_FEAT_DIM
    return max(abs(x - y) for x, y in zip(a[:n], b[:n])) < 1e-3


def attach_nstep_returns(experiences: list[dict], log: Optional[logging.Logger] = None) -> list[dict]:
    """把每筆（決策）經驗的 reward 改成 n 步折扣和、next_state 改成第 m 步之後的狀態、boot_discount＝γ^m（m≤n，
    遇到時間中斷或資料尾端就截斷）。依時間戳排序；不改變經驗筆數。"""
    if NSTEP <= 1 or not experiences:
        return experiences
    exps = sorted((e for e in experiences if e.get("timestamp") is not None), key=lambda e: e["timestamp"])
    rest = [e for e in experiences if e.get("timestamp") is None]
    r1 = [float(e["reward"]) for e in exps]
    nxt = [(e["next_state_vec"], e["next_mask_vec"]) for e in exps]
    ts = [e["timestamp"].timestamp() for e in exps]
    out = []
    msum = 0
    for k, e in enumerate(exps):
        g, m = r1[k], 1
        while (m < NSTEP and k + m < len(exps) and ts[k + m] - ts[k + m - 1] <= NSTEP_MAX_GAP_S
               and _contiguous(exps[k + m - 1], exps[k + m])):
            g += (NSTEP_GAMMA ** m) * r1[k + m]
            m += 1
        f = dict(e)
        f["reward1"] = r1[k]
        f["reward"] = g
        f["next_state_vec"], f["next_mask_vec"] = nxt[k + m - 1]
        f["boot_discount"] = NSTEP_GAMMA ** m
        msum += m
        out.append(f)
    if log:
        log.info("n 步回報（n=%d，γ=%.2f）：%d 筆，平均實際步數 %.2f", NSTEP, NSTEP_GAMMA, len(out), msum / max(len(out), 1))
    return out + rest


def merge_action_holds(experiences: list[dict], log: Optional[logging.Logger] = None) -> list[dict]:
    """v3.3 動作持續：把同一個 hold_id 的逐秒經驗（時間升序）併成一筆決策經驗——state／動作／behavior_logp 取決策當下
    （hold_pos=0）那一筆，reward 取各秒 reward 平均，next_state 取最後一秒（＝下一個決策當下的狀態）。缺決策當下那一筆的
    群組（被 fetch 上限截斷或 reward 對不到子節點而丟棄）整組捨棄。沒有 hold_id 的經驗原樣保留。"""
    if not any(e.get("hold_id") for e in experiences):
        return experiences
    out: list[dict] = []
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for e in experiences:
        h = e.get("hold_id")
        if not h:
            out.append(e)
            continue
        if h not in groups:
            groups[h] = []
            order.append(h)
        groups[h].append(e)
    dropped = 0
    for h in order:
        g = groups[h]
        head = next((e for e in g if e.get("hold_pos") == 0), None)
        # 只接受完整區段（位置 0..K−1 都在；2026-10-04 Codex 審查：原本只要有 hold_pos=0 就接受）
        if head is None or (HOLD_K > 1 and sorted(e.get("hold_pos", -1) for e in g) != list(range(HOLD_K))):
            dropped += 1
            continue
        g.sort(key=lambda e: e.get("hold_pos", 0))
        m = dict(head)
        m["reward"] = float(sum(e["reward"] for e in g) / len(g))
        m["next_state_vec"], m["next_mask_vec"] = g[-1]["next_state_vec"], g[-1]["next_mask_vec"]
        m["hold_n"] = len(g)
        out.append(m)
    if log:
        log.info("動作持續：%d 筆逐秒經驗併成 %d 筆決策經驗（缺決策當下而捨棄 %d 組）",
                 sum(len(groups[h]) for h in order), len(out), dropped)
    return out


def fetch_sequences(
    mongo_col: pymongo.collection.Collection,
    fetch_limit: int = TRAIN_FETCH_LIMIT,
    seq_len: int = TRAIN_SEQ_LEN,
    log: Optional[logging.Logger] = None,
) -> tuple[list[list[dict]], int]:
    """
    MODEL_ARCH=gru 專用。從 MongoDB 讀取「最新」的 fetch_limit 筆經驗（回傳前還原成時間升序），
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
            .sort("timestamp", pymongo.DESCENDING)   # 最新的 N 筆（舊版 ASCENDING 取到最早的 N 筆）
            .limit(fetch_limit)
        )
        experiences = list(cursor)
        experiences.reverse()   # 還原成時間升序，下面依序判斷連續性
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
    # 可更新 Actor 的壅塞樣本數：FL 客戶端拿它當 FedAvg 權重（見 client_app.py）
    with (lock if lock is not None else contextlib.nullcontext()):
        result["n_contended_train"] = agent.count_contended(train_exp)
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
