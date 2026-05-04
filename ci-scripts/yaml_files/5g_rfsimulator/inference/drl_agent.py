"""
drl_agent.py — DRL Actor-Critic Agent for Local PRB Allocation

架構：
  - Actor Network : MLP + Masked Softmax → PRB 分配比例
  - Critic Network: MLP → 狀態價值估計 V(s)
  - 演算法        : 離線 Advantage Actor-Critic (A2C)
                    從 MongoDB 讀取歷史經驗進行批次更新

State Space (固定長度向量，不足補零)：
  [norm_bsr_0, norm_cqi_0, norm_bsr_1, norm_cqi_1, ..., active_ratio]
  長度 = MAX_UE_COUNT * 2 + 1 = 33

Action Space：
  各 UE 的 PRB 分配比例 [0, 1]，總和為 1.0
  非活躍 UE slot 的比例透過 mask 強制為 0。

訓練方式：
  InferenceServer 的背景執行緒每 TRAIN_INTERVAL_S 秒呼叫 train_on_batch()，
  從 MongoDB 取得最近 N 筆 (state, action, reward, next_state) 進行梯度更新。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

# =============================================================================
# 超參數
# =============================================================================

MAX_UE_COUNT: int = 16
STATE_DIM: int = MAX_UE_COUNT * 2 + 1   # [bsr, cqi] × N + active_ratio

MAX_BSR: float = 100_000.0              # DL delta-TBS 正規化上限 (bytes/10ms, ≈80 Mbps)
GAMMA: float = 0.95                      # 折扣因子

LR_ACTOR: float = 1e-4
LR_CRITIC: float = 3e-4
HIDDEN_DIM: int = 128
TRAIN_BATCH_SIZE: int = 128
MIN_TRAIN_EXPERIENCES: int = 200         # 觸發第一次訓練所需的最少經驗數
DIRICHLET_CONCENTRATION: float = 5.0    # Dirichlet 策略集中度 K：α = probs × K
                                         # K 越大越確定性，K 越小探索性越強


# =============================================================================
# 神經網路定義
# =============================================================================

class ActorNetwork(nn.Module):
    """
    Policy Network：state → PRB 分配 logits → Masked Softmax。

    非活躍 UE slot 在 softmax 前被設為 -∞，確保輸出比例為 0。
    """

    def __init__(
        self,
        state_dim: int = STATE_DIM,
        max_ues: int = MAX_UE_COUNT,
    ) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(state_dim, HIDDEN_DIM),
            nn.ReLU(),
            nn.Linear(HIDDEN_DIM, HIDDEN_DIM),
            nn.ReLU(),
            nn.Linear(HIDDEN_DIM, max_ues),
        )

    def forward(
        self,
        state: torch.Tensor,   # (batch, state_dim)
        mask: torch.Tensor,    # (batch, max_ues)  True = 活躍 UE
    ) -> torch.Tensor:
        """回傳各 UE 的 PRB 分配比例，形狀 (batch, max_ues)。"""
        logits = self.net(state)                    # (batch, max_ues)
        logits = logits.masked_fill(~mask, -1e9)   # 遮蔽非活躍 slot
        return F.softmax(logits, dim=-1)            # (batch, max_ues)


class CriticNetwork(nn.Module):
    """Value Network：state → scalar V(s)。"""

    def __init__(self, state_dim: int = STATE_DIM) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(state_dim, HIDDEN_DIM),
            nn.ReLU(),
            nn.Linear(HIDDEN_DIM, 64),
            nn.ReLU(),
            nn.Linear(64, 1),
        )

    def forward(self, state: torch.Tensor) -> torch.Tensor:
        """回傳狀態價值估計，形狀 (batch,)。"""
        return self.net(state).squeeze(-1)


# =============================================================================
# DRLAgent
# =============================================================================

class DRLAgent:
    """
    PRB 分配的 Actor-Critic DRL Agent。

    典型使用流程：
        agent = DRLAgent(node_id=1)
        agent.load()                     # 嘗試載入預存權重

        # 每 10ms 推論一次（由 InferenceServer 呼叫）
        allocations = agent.infer(ues)

        # 每 TRAIN_INTERVAL_S 秒訓練一次（由背景執行緒呼叫）
        metrics = agent.train_on_batch(experiences)
        agent.save()
    """

    def __init__(
        self,
        node_id: int,
        model_dir: str = "/app/models",
        total_prb: int = 106,
        device: Optional[str] = None,
    ) -> None:
        self.node_id = node_id
        self.model_dir = Path(model_dir)
        self.model_dir.mkdir(parents=True, exist_ok=True)
        self.total_prb = total_prb
        self.device = torch.device(
            device if device else ("cuda" if torch.cuda.is_available() else "cpu")
        )

        self.actor = ActorNetwork().to(self.device)
        self.critic = CriticNetwork().to(self.device)
        self.actor_opt = optim.Adam(self.actor.parameters(), lr=LR_ACTOR)
        self.critic_opt = optim.Adam(self.critic.parameters(), lr=LR_CRITIC)

        # _is_trained=False 時 InferenceServer 退回 BSR 啟發式
        self._is_trained: bool = False
        self._train_steps: int = 0

        self._log = logging.getLogger(f"drl_agent_node{node_id}")

    # -------------------------------------------------------------------------
    # 狀態編碼
    # -------------------------------------------------------------------------

    def encode_state(
        self,
        ues: list[dict],
    ) -> tuple[np.ndarray, np.ndarray]:
        """
        將 UE 列表編碼為固定長度的 numpy 向量。

        回傳：
            state_vec : (STATE_DIM,)  float32
            mask_vec  : (MAX_UE_COUNT,) bool，True = 活躍 UE
        """
        n = min(len(ues), MAX_UE_COUNT)
        state_vec = np.zeros(STATE_DIM, dtype=np.float32)
        mask_vec = np.zeros(MAX_UE_COUNT, dtype=bool)

        for i, ue in enumerate(ues[:n]):
            bsr = float(ue.get("bsr", 0))          # delta_dl_aggr_tbs (bytes)
            mcs = float(ue.get("wb_cqi", 0))       # dl_mcs1 (0-28, mapped to wb_cqi key)
            # Log 正規化 delta TBS → [0, 1]
            state_vec[i * 2]     = np.log1p(bsr) / np.log1p(MAX_BSR)
            # 正規化 MCS → [0, 1]（MCS=0 合法，反映低通道品質）
            state_vec[i * 2 + 1] = mcs / 28.0
            mask_vec[i] = True

        # 活躍 UE 比例作為全域 context 特徵
        state_vec[MAX_UE_COUNT * 2] = n / MAX_UE_COUNT

        return state_vec, mask_vec

    def _to_tensors(
        self,
        state_vec: np.ndarray,
        mask_vec: np.ndarray,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """將 numpy 向量轉換為 (1, *) 的 batch tensor。"""
        state_t = torch.tensor(
            state_vec, dtype=torch.float32, device=self.device
        ).unsqueeze(0)
        mask_t = torch.tensor(
            mask_vec, dtype=torch.bool, device=self.device
        ).unsqueeze(0)
        return state_t, mask_t

    # -------------------------------------------------------------------------
    # 推論
    # -------------------------------------------------------------------------

    def infer(self, ues: list[dict]) -> tuple[list[dict], np.ndarray]:
        """
        執行 DRL Actor 推論，回傳 PRB 分配結果與比例向量。

        Args:
            ues : [{"rnti": int, "bsr": int, "wb_cqi": int}, ...]

        Returns:
            allocations   : [{"rnti": int, "prb_abs": int}, ...]
            action_ratios : (MAX_UE_COUNT,) float32，用於 MongoDB 儲存
        """
        if not ues:
            return [], np.zeros(MAX_UE_COUNT, dtype=np.float32)

        n = min(len(ues), MAX_UE_COUNT)
        state_vec, mask_vec = self.encode_state(ues)
        state_t, mask_t = self._to_tensors(state_vec, mask_vec)

        self.actor.eval()
        with torch.no_grad():
            probs = self.actor(state_t, mask_t)[0]          # (MAX_UE_COUNT,) on device

            # Dirichlet 隨機策略：從 Dirichlet(α = probs[:n] × K) 採樣
            # 確保 action_ratios ≠ actor probs，訓練時 log π(a|s) 梯度有效
            alpha = torch.clamp(probs[:n] * DIRICHLET_CONCENTRATION, min=1e-3)
            dist = torch.distributions.Dirichlet(alpha)
            active_ratios = dist.sample().cpu().numpy()     # (n,)，加總恰好為 1

        # 轉換為整數 PRB，修正捨入誤差
        prb_floats = active_ratios * self.total_prb
        prb_ints = prb_floats.astype(np.int32)
        remainder = int(self.total_prb - prb_ints.sum())
        if remainder > 0:
            fracs = prb_floats - prb_ints
            top_idx = int(np.argmax(fracs))
            prb_ints[top_idx] += remainder

        # 確保每個活躍 UE 至少分配 1 個 PRB
        for i in range(n):
            if prb_ints[i] < 1:
                prb_ints[i] = 1

        allocations = [
            {"rnti": int(ues[i]["rnti"]), "prb_abs": int(prb_ints[i])}
            for i in range(n)
        ]

        # 儲存 Dirichlet 採樣值（四捨五入前），供訓練時計算 log π(a|s)
        action_ratios = np.zeros(MAX_UE_COUNT, dtype=np.float32)
        action_ratios[:n] = active_ratios

        return allocations, action_ratios

    # -------------------------------------------------------------------------
    # 離線訓練
    # -------------------------------------------------------------------------

    def train_on_batch(self, experiences: list[dict]) -> dict:
        """
        從 MongoDB 取得的經驗批次進行 Actor-Critic 離線更新。

        Experience document schema：
          {
            "state_vec"     : list[float],  長度 STATE_DIM
            "mask_vec"      : list[bool],   長度 MAX_UE_COUNT
            "action_ratios" : list[float],  長度 MAX_UE_COUNT
            "reward"        : float,
            "next_state_vec": list[float],  長度 STATE_DIM
            "next_mask_vec" : list[bool],   長度 MAX_UE_COUNT
          }

        Returns:
            metrics : dict with training statistics
        """
        if len(experiences) < TRAIN_BATCH_SIZE:
            self._log.info(
                "經驗數量不足 (有 %d 筆，需 %d 筆)，跳過訓練",
                len(experiences), TRAIN_BATCH_SIZE,
            )
            return {}

        # 隨機取樣一個 mini-batch
        idxs = np.random.choice(len(experiences), TRAIN_BATCH_SIZE, replace=False)
        batch = [experiences[i] for i in idxs]

        # ── 建立 Tensor ────────────────────────────────────────────────────
        states = torch.tensor(
            np.array([e["state_vec"]       for e in batch], dtype=np.float32),
            device=self.device,
        )
        masks = torch.tensor(
            np.array([e["mask_vec"]        for e in batch], dtype=bool),
            device=self.device,
        )
        actions = torch.tensor(
            np.array([e["action_ratios"]   for e in batch], dtype=np.float32),
            device=self.device,
        )
        rewards = torch.tensor(
            np.array([e["reward"]          for e in batch], dtype=np.float32),
            device=self.device,
        )
        next_states = torch.tensor(
            np.array([e["next_state_vec"]  for e in batch], dtype=np.float32),
            device=self.device,
        )
        next_masks = torch.tensor(
            np.array([e["next_mask_vec"]   for e in batch], dtype=bool),
            device=self.device,
        )

        # ── Critic 更新 (最小化 TD 誤差) ──────────────────────────────────
        self.critic.train()
        with torch.no_grad():
            next_values = self.critic(next_states)           # V(s')
            targets = rewards + GAMMA * next_values          # TD target

        current_values = self.critic(states)                 # V(s)
        critic_loss = F.mse_loss(current_values, targets)

        self.critic_opt.zero_grad()
        critic_loss.backward()
        torch.nn.utils.clip_grad_norm_(self.critic.parameters(), 1.0)
        self.critic_opt.step()

        # ── Actor 更新 (Dirichlet Policy Gradient) ────────────────────────
        self.actor.train()
        with torch.no_grad():
            advantages = (targets - self.critic(states)).detach()
            if advantages.std() > 1e-8:
                advantages = (advantages - advantages.mean()) / (
                    advantages.std() + 1e-8
                )

        probs = self.actor(states, masks)              # (batch, MAX_UE_COUNT)

        # 對每個樣本分別計算 Dirichlet log π(a|s) 與 entropy
        # 每個樣本的活躍 UE 數量不同，需逐一處理
        log_probs_list: list[torch.Tensor] = []
        entropy_list: list[torch.Tensor] = []
        for i in range(len(batch)):
            n_i = int(masks[i].sum().item())
            if n_i == 0:
                log_probs_list.append(torch.tensor(0.0, device=self.device))
                entropy_list.append(torch.tensor(0.0, device=self.device))
                continue

            alpha_i = torch.clamp(
                probs[i, :n_i] * DIRICHLET_CONCENTRATION, min=1e-3
            )
            dist_i = torch.distributions.Dirichlet(alpha_i)

            # 取出此樣本的儲存動作（active UE 子集），正規化確保加總為 1
            a_i = actions[i, :n_i]
            a_sum = a_i.sum()
            if a_sum < 1e-8:
                log_probs_list.append(torch.tensor(0.0, device=self.device))
                entropy_list.append(dist_i.entropy())
                continue
            a_i = torch.clamp(a_i / a_sum, min=1e-6)
            a_i = a_i / a_i.sum()

            log_probs_list.append(dist_i.log_prob(a_i))
            entropy_list.append(dist_i.entropy())

        log_probs_t = torch.stack(log_probs_list)    # (batch,)
        entropy_t   = torch.stack(entropy_list)       # (batch,)

        actor_loss = -(advantages * log_probs_t).mean()
        entropy_coeff = max(0.001, 0.01 * (0.997 ** self._train_steps))
        actor_loss = actor_loss - entropy_coeff * entropy_t.mean()

        self.actor_opt.zero_grad()
        actor_loss.backward()
        torch.nn.utils.clip_grad_norm_(self.actor.parameters(), 1.0)
        self.actor_opt.step()

        self._train_steps += 1
        self._is_trained = True

        metrics = {
            "train_step":  self._train_steps,
            "actor_loss":  float(actor_loss.item()),
            "critic_loss": float(critic_loss.item()),
            "entropy":     float(entropy_t.mean().item()),
            "mean_reward": float(rewards.mean().item()),
            "mean_adv":    float(advantages.mean().item()),
        }
        self._log.info(
            "[訓練] step=%d actor_loss=%.4f critic_loss=%.4f "
            "entropy=%.4f mean_reward=%.4f",
            self._train_steps,
            metrics["actor_loss"],
            metrics["critic_loss"],
            metrics["entropy"],
            metrics["mean_reward"],
        )
        return metrics

    @property
    def is_trained(self) -> bool:
        """是否已完成至少一次訓練，可切換至 DRL 推論模式。"""
        return self._is_trained

    # -------------------------------------------------------------------------
    # 模型持久化
    # -------------------------------------------------------------------------

    def save(self) -> None:
        """將 Actor + Critic 的權重與優化器狀態存至磁碟。"""
        path = self.model_dir / f"model_node{self.node_id}.pt"
        torch.save(
            {
                "actor":       self.actor.state_dict(),
                "critic":      self.critic.state_dict(),
                "actor_opt":   self.actor_opt.state_dict(),
                "critic_opt":  self.critic_opt.state_dict(),
                "train_steps": self._train_steps,
            },
            path,
        )
        self._log.info("模型已儲存至 %s (訓練步數: %d)", path, self._train_steps)

    def load(self) -> bool:
        """
        嘗試從磁碟載入預存權重。

        Returns:
            True 表示成功載入，False 表示找不到檔案或載入失敗（從隨機初始化開始）。
        """
        path = self.model_dir / f"model_node{self.node_id}.pt"
        if not path.exists():
            self._log.info("未找到預存模型 (%s)，從隨機初始化開始", path)
            return False
        try:
            ckpt = torch.load(path, map_location=self.device)
            self.actor.load_state_dict(ckpt["actor"])
            self.critic.load_state_dict(ckpt["critic"])
            self.actor_opt.load_state_dict(ckpt["actor_opt"])
            self.critic_opt.load_state_dict(ckpt["critic_opt"])
            self._train_steps = ckpt.get("train_steps", 0)
            self._is_trained  = self._train_steps > 0
            self._log.info(
                "模型已從 %s 載入 (訓練步數: %d)", path, self._train_steps
            )
            return True
        except Exception as exc:
            self._log.warning("模型載入失敗: %s，從隨機初始化開始", exc)
            return False
