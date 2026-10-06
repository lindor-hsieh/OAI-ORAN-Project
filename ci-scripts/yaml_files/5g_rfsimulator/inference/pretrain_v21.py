"""
pretrain_v21.py — Local DRL v2.1 的離線初始化（LOCAL_DRL_V2_DESIGN.md §10.2c）

用動態規則模式（XAPP_MODE=rule、RULE_KIND=dyn）收集的 node{N}_pf_shadow 資料：
  1. Actor：以規則的決策（文件的 rule_macro）做行為複製；
  2. Critic：V(s) ≈ 規則策略下的 E[r|s]（reward 定義與線上相同，相鄰兩筆配對，同 pretrain_critic.build_dataset）。
兩者都用時間切分的保留集（最後 --eval-frac）評估，不參與訓練；最後用全部資料再訓練一次存檔。

另外回報「規則遮的 UE」與「宏動作候選排序」的一致率：宏動作只決定要不要遮、遮多重，遮誰由 MCS 排序決定，
一致率低代表 BC 標籤（遮幾個）對應到的 UE 跟規則實際遮的不同，需要檢查。

用法（PC1，該節點 inference 容器內）：
  docker exec inference-node1 python pretrain_v21.py --node 1 --scenario-tag hs_rule_train
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pymongo
import torch

from drl_agent import ACTION_SPACE, MAX_UE_COUNT, MODEL_ARCH, STATE_DIM, DRLAgent, macro_context
from pretrain_critic import STALE_GAP_S, build_dataset


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", type=int, required=True)
    ap.add_argument("--scenario-tag", required=True, help="規則資料的 PF_SHADOW_SCENARIO_TAG，可逗號列多個")
    ap.add_argument("--eval-frac", type=float, default=0.2)
    ap.add_argument("--mongo-uri", default=os.environ.get("MONGO_URI", "mongodb://localhost:27017"))
    ap.add_argument("--mongo-db", default=os.environ.get("MONGO_DB", "iab_xapp"))
    ap.add_argument("--model-dir", default=os.environ.get("MODEL_DIR", "/app/models"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if MODEL_ARCH != "mlp" or ACTION_SPACE != "macro":
        print(f"[pretrain_v21] 需要 MODEL_ARCH=mlp、DRL_ACTION_SPACE=macro（目前 {MODEL_ARCH}／{ACTION_SPACE}）")
        return 1

    coll = pymongo.MongoClient(args.mongo_uri)[args.mongo_db][f"node{args.node}_pf_shadow"]
    tags = [t.strip() for t in args.scenario_tag.split(",") if t.strip()]
    proj = {"_id": 0, "timestamp": 1, "t_mono": 1, "state_vec": 1, "mask_vec": 1, "ues": 1,
            "rule_macro": 1, "rule_masked_idx": 1}
    docs = [d for d in coll.find({"scenario_tag": {"$in": tags}}, proj).sort("timestamp", 1)
            if len(d.get("state_vec", [])) == STATE_DIM and d.get("rule_macro") is not None]
    if len(docs) < 500:
        print(f"[pretrain_v21] node{args.node} 規則資料只有 {len(docs)} 筆，中止")
        return 1

    S = np.asarray([d["state_vec"] for d in docs], dtype=np.float32)
    M = np.asarray([d["mask_vec"] for d in docs], dtype=bool)
    Y = np.asarray([int(d["rule_macro"]) for d in docs], dtype=np.int64)

    # 規則實際遮的 UE vs 宏動作候選排序
    mc = macro_context(torch.tensor(S), torch.tensor(M))
    cand = mc["cand"].numpy()
    agree = n_mask = 0
    for d, c, y in zip(docs, cand, Y):
        if y == 0:
            continue
        n_mask += 1
        want = set(int(x) for x in c[:1 if y == 2 else 2] if x >= 0)
        agree += set(d.get("rule_masked_idx", [])) == want
    print(f"[pretrain_v21] node{args.node} 文件 {len(docs)}｜標籤分布 {np.bincount(Y, minlength=4).tolist()}｜"
          f"規則遮的 UE＝候選排序 {agree}/{n_mask}（{agree / max(n_mask, 1):.0%}）｜決策點 "
          f"{int((mc['starved'] & (mc['n_cand'] >= 1)).sum())}")

    cut = int(len(docs) * (1.0 - args.eval_frac))
    # ── 保留集評估（訓練前 80%、評估最後 20%）──
    ev = DRLAgent(node_id=args.node, model_dir="/tmp/pretrain_v21_eval")
    r_bc = ev.bc_pretrain_actor(S, M, Y, holdout_frac=args.eval_frac)
    print(f"[pretrain_v21] BC 時間保留集：整體一致率 {r_bc['acc_holdout']:.1%}、決策點一致率 "
          f"{r_bc['acc_holdout_decision']:.1%}（n={r_bc['n_holdout_decision']}）、訓練集 {r_bc['acc_train']:.1%}")
    st_tr, mk_tr, rw_tr, stats_tr = build_dataset(docs[:cut])
    st_ho, mk_ho, rw_ho, _ = build_dataset(docs[cut:])
    ev.pretrain_critic(st_tr, mk_tr, rw_tr)
    with torch.no_grad():
        v = ev.critic(torch.tensor(st_ho), torch.tensor(mk_ho)).numpy()
    mse = float(((v - rw_ho) ** 2).mean()); base = float(((rw_ho - rw_tr.mean()) ** 2).mean())
    print(f"[pretrain_v21] Critic 時間保留集：MSE={mse:.5f} 永遠猜平均={base:.5f} 解釋比例={1 - mse / base:.1%}｜{stats_tr}")

    if args.dry_run:
        return 0
    # ── 全部資料重新訓練並存檔（不載入舊 checkpoint）──
    ag = DRLAgent(node_id=args.node, model_dir=args.model_dir)
    r_all = ag.bc_pretrain_actor(S, M, Y, holdout_frac=0.0001)
    st, mk, rw, _ = build_dataset(docs)
    ag.pretrain_critic(st, mk, rw)
    ag._is_trained = True
    ag.save()
    print(f"[pretrain_v21] node{args.node} 已存檔（全資料 BC 一致率 {r_all['acc_train']:.1%}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
