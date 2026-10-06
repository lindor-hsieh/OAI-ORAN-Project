"""
pretrain_critic.py — Local DRL v2 的 Critic 離線預訓練（LOCAL_DRL_V2_DESIGN.md §1）

用 PF-shadow 模式（XAPP_MODE=shadow）收集的 node{N}_pf_shadow 資料，訓練 V(s) ≈ PF 策略下的 E[r|s]，
存成線上訓練的初始 checkpoint：Actor 保持≈PF 的初始化（ActorNetworkMLP 的 bias 初始化）、Critic
換成預訓練結果。線上訓練一開始策略≈PF、V(s)≈PF 的表現，advantage = r − V(s) 天然代表「比 PF 好多少」。

reward 定義與線上完全相同（inference_server.py::_build_rl_experience() 的 throughput_only 路徑）：
第 t 筆的 reward 用第 t+1 筆的 UE 狀態算 compute_reward_breakdown(ues_{t+1}, PF 分配)。相鄰兩筆
間隔超過 STALE_GAP_S（容器重啟/中斷）就不配對，同線上 STALE_PREV_UES_THRESHOLD_S 的規則。

用法（在 PC1、該節點的 inference 容器內執行，模型 volume 掛在 /app/models）：
  docker exec inference-node1 python pretrain_critic.py --node 1 --scenario-tag t
  # 或一次全部 12 個節點（容器外）：
  for n in $(seq 1 12); do docker exec inference-node$n python pretrain_critic.py --node $n --scenario-tag t; done
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pymongo

from drl_agent import MAX_UE_COUNT, MODEL_ARCH, STATE_DIM, DRLAgent
from reward_calculator import compute_reward_breakdown

TOTAL_PRB: int = 106
STALE_GAP_S: float = 5.0   # 同 inference_server.py 的 STALE_PREV_UES_THRESHOLD_S


def build_dataset(docs: list[dict]) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict]:
    """把依時間排序的 PF-shadow 文件兩兩配對成 (state_t, mask_t, r_t)。回傳 (states, masks, rewards, 統計)。"""
    states: list[list[float]] = []
    masks: list[list[bool]] = []
    rewards: list[float] = []
    n_gap = n_dim = 0
    for a, b in zip(docs, docs[1:]):
        if len(a.get("state_vec", [])) != STATE_DIM or len(a.get("mask_vec", [])) != MAX_UE_COUNT:
            n_dim += 1
            continue
        dt_wall = (b["timestamp"] - a["timestamp"]).total_seconds()
        dt_mono = float(b.get("t_mono", 0.0)) - float(a.get("t_mono", 0.0))
        # 牆鐘間隔或單調時鐘不連續（容器重啟後 t_mono 會重置）→ 中間發生過中斷，不配對
        if not (0.0 < dt_wall <= STALE_GAP_S and 0.0 < dt_mono <= STALE_GAP_S):
            n_gap += 1
            continue
        pf_alloc = [{"rnti": int(u["rnti"]), "prb_abs": TOTAL_PRB} for u in a.get("ues", [])]
        r = compute_reward_breakdown(b.get("ues", []), pf_alloc, total_prb=TOTAL_PRB)["reward"]
        states.append(a["state_vec"])
        masks.append(a["mask_vec"])
        rewards.append(float(r))
    stats = {"n_docs": len(docs), "n_pairs": len(rewards), "skipped_gap": n_gap, "skipped_dim": n_dim}
    return (np.asarray(states, dtype=np.float32).reshape(-1, STATE_DIM),
            np.asarray(masks, dtype=bool).reshape(-1, MAX_UE_COUNT),
            np.asarray(rewards, dtype=np.float32), stats)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--node", type=int, required=True, help="節點編號 1~12")
    ap.add_argument("--scenario-tag", required=True,
                    help="訓練資料的場景家族（PF_SHADOW_SCENARIO_TAG），可用逗號列多個，例如 tm 或 tm,t,th")
    ap.add_argument("--eval-tag", default=None,
                    help="依時間切出這個場景家族最後 --eval-frac 的資料當保留集（不參與訓練），報告時間外推的 MSE")
    ap.add_argument("--eval-frac", type=float, default=0.2)
    ap.add_argument("--epochs", type=int, default=50)
    ap.add_argument("--mongo-uri", default=os.environ.get("MONGO_URI", "mongodb://localhost:27017"))
    ap.add_argument("--mongo-db", default=os.environ.get("MONGO_DB", "iab_xapp"))
    ap.add_argument("--model-dir", default=os.environ.get("MODEL_DIR", "/app/models"))
    ap.add_argument("--dry-run", action="store_true", help="只統計資料、訓練並印結果，不寫 checkpoint")
    args = ap.parse_args()

    if MODEL_ARCH != "mlp":
        print(f"[pretrain_critic] 只支援 Local DRL v2（MODEL_ARCH=mlp），目前是 {MODEL_ARCH}")
        return 1

    coll = pymongo.MongoClient(args.mongo_uri)[args.mongo_db][f"node{args.node}_pf_shadow"]
    proj = {"_id": 0, "timestamp": 1, "t_mono": 1, "state_vec": 1, "mask_vec": 1, "ues": 1}
    parts, eval_set = [], None
    # 每個場景家族各自配對（不同家族分屬不同時段，跨家族的相鄰兩筆本來就會因間隔過大被跳過，分開算較清楚）。
    # 每秒一筆的資料前後高度相關，隨機抽保留集會跟訓練集幾乎重複、誤差被低估 → 用時間切分的保留集評估外推能力。
    for tag in [t.strip() for t in args.scenario_tag.split(",") if t.strip()]:
        docs = list(coll.find({"scenario_tag": tag}, proj).sort("timestamp", 1))
        if tag == args.eval_tag and docs:
            cut = int(len(docs) * (1.0 - args.eval_frac))
            eval_set = build_dataset(docs[cut:])
            docs = docs[:cut]
        st_, mk_, rw_, stats = build_dataset(docs)
        print(f"[pretrain_critic] node{args.node} scenario={tag} {stats}")
        parts.append((st_, mk_, rw_))
    if args.eval_tag and eval_set is None:
        eval_docs = list(coll.find({"scenario_tag": args.eval_tag}, proj).sort("timestamp", 1))
        eval_set = build_dataset(eval_docs[int(len(eval_docs) * (1.0 - args.eval_frac)):])
    states = np.concatenate([p[0] for p in parts]) if parts else np.zeros((0, STATE_DIM), np.float32)
    masks = np.concatenate([p[1] for p in parts]) if parts else np.zeros((0, MAX_UE_COUNT), bool)
    rewards = np.concatenate([p[2] for p in parts]) if parts else np.zeros(0, np.float32)
    if len(rewards) == 0:
        print("[pretrain_critic] 沒有可用的配對資料，中止")
        return 1
    print(f"[pretrain_critic] reward mean={rewards.mean():.4f} std={rewards.std():.4f} "
          f"idle_frac={(rewards < 1e-9).mean():.2%}")

    # 不載入既有 checkpoint：預訓練是線上訓練前的第一步，Actor 必須是乾淨的 PF 初始化
    agent = DRLAgent(node_id=args.node, model_dir=args.model_dir)
    result = agent.pretrain_critic(states, masks, rewards, epochs=args.epochs)
    print(f"[pretrain_critic] {result}")
    if eval_set is not None and len(eval_set[2]) > 0:
        import torch
        es, em, er = (torch.tensor(eval_set[0]), torch.tensor(eval_set[1]), torch.tensor(eval_set[2]))
        with torch.no_grad():
            v = agent.critic(es.to(agent.device), em.to(agent.device)).cpu()
        mse = float(((v - er) ** 2).mean())
        base = float(((er - float(rewards.mean())) ** 2).mean())
        print(f"[pretrain_critic] 時間外推保留集（{args.eval_tag} 最後 {args.eval_frac:.0%}，n={len(er)}）："
              f"MSE={mse:.5f}  永遠猜訓練平均的 MSE={base:.5f}  解釋比例={1 - mse / base if base > 0 else float('nan'):.1%}")
    if result["holdout_mse"] >= result["holdout_baseline_mse"]:
        print("[pretrain_critic] 警告：Critic 在 holdout 上沒有比『永遠猜平均』更好，"
              "代表沒學到 state 與 reward 的關係——先檢查資料量與場景是否有壅塞，再決定要不要用這份 checkpoint")

    if args.dry_run:
        print("[pretrain_critic] --dry-run：不寫 checkpoint")
        return 0
    agent.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
