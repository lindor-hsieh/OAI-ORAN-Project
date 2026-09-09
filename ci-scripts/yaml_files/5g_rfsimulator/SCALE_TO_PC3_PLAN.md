# 擴展至三台實體主機（新增 Node6/7/8）計劃書

> 狀態：**規劃中，尚未實作**。物理連接方案尚未決定（switch vs 第二張網卡），需要你先確認
> 才能定案後續的 IP/連接埠配置。Node6/7/8 的拓撲角色已確認：跟 Node3/4/5 一樣當 access
> node，但「掛在哪個 relay 底下」還有一個需要你決定的設計分歧，見 §3.2。

---

## 0. 現況盤點（先弄清楚牽動範圍多大）

查了程式碼，發現「5 個節點／6 條 E2 連線」這個假設寫死在至少 5 個不同層級，這代表加
PC3 **不是單純多接一台機器**，是要同時動：實體網路、docker-compose 位址配置、C xApp、
Python DRL 狀態維度、Global 配額邏輯、FL 聚合設定、啟動/監控腳本。逐一列出：

| 層級 | 檔案 | 寫死的假設 |
|---|---|---|
| 實體網路 | （硬體本身） | PC1↔PC2 用專用 USB 網卡直接接線，`macvlan` 需要同一 L2 廣播網域 |
| 啟動腳本 | `iab/run_local_pc1.sh`、`iab/monitor_drl.sh`、`iab/start_iab_client.sh` | E2 連線數檢查寫死 `/6` |
| Global 配額 | `inference/global_xapp.py` 的 `compute_quotas()` | Node3/4 配額 = Node1 兩個 MT 的分配（依 rnti 排序取前兩個）；Node5 配額 = Node2 唯一 MT 的分配總和 |
| 訓練場景 | `scenarios/traffic_scenario.py` 的 `NODE_CONFIG` | 只列了 Node3/4/5 |
| 聯邦學習 | `inference/flower-app/iab_fl/server_app.py` | `NUM_NODES=5`，`min_train_nodes=min_evaluate_nodes=min_available_nodes=5` |
| xApp 開發規範 | CLAUDE.md §7 | 新增 xApp 需要先編譯 FlexRIC xApp + OAI RAN with E2 Agent，編譯過了才能審核，不能一次性大改跳過驗證 |

---

## 1. 物理連接：兩種方案，先看步驟再決定

目前 PC1（`enxc84d44350030`）與 PC2（`enxc84d44350008`）用專用 USB 網卡直接接線，
`docker-compose-*.yaml` 的 macvlan 設定把整個 `192.168.88.0/24` 網段建立在這張網卡上。
macvlan **需要所有參與的主機在同一個 L2 廣播網域**，這是物理連接方式選擇的根本限制。

### 方案 A：買一台網路交換器（推薦）

三台機器的專用網卡（或新買的網卡）都接到同一台 switch，維持單一 `192.168.88.0/24`
網段，PC3 比照 PC2 現有模式接入即可。

**優點**：
- 現有 macvlan 架構完全不用改，`parent:` 介面名稱不變（假設用同一張實體網卡接 switch）
- 不需要在 PC1 上加裝第二張網卡
- 不需要處理跨網段路由，CU DNAT 邏輯（現有的已知痛點，見 CLAUDE.md §7「IAB 啟動順序
  與 CU DNAT 陷阱」）不用擴充成多網段版本
- 之後如果還要加 PC4、PC5，一樣接同一台 switch 就好，擴充性最好

**步驟**：
1. 買一台基本的 Gigabit 交換器（8 port 以上即可，不需要管理型）
2. 三台 PC 原本接彼此的網路線都改接到 switch
3. 確認三張網卡的 `ip link show` 都能看到彼此（`arping`／`ping` 192.168.88.x 測試）
4. macvlan 設定完全不用改，`docker-compose-iab-server.yaml`／`docker-compose-iab-client.yaml`
   的 `parent:` 介面名稱維持不變（除非交換器換了實體網卡才需要改名）

**代價**：需要買硬體（一般 Gigabit switch 成本低，但仍是一筆額外開銷跟等待時間）。

### 方案 B：PC1 加裝第二張網卡，各自點對點直連

PC1 變成 hub，分別用兩條獨立的網路線接到 PC2 跟 PC3，PC1 上要有兩張獨立網卡各自負責
一條連線，兩個獨立子網（例如 PC1↔PC2 維持 `192.168.88.0/24`，PC1↔PC3 新開
`192.168.89.0/24`），PC1 中間要做路由 + NAT/DNAT 轉發。

**優點**：不用買額外硬體（如果 PC1 剛好有空的 USB 埠或內建網卡）。

**代價（明顯比方案 A 大）**：
- PC1 需要一張額外網卡（USB 網卡或內建），且 macvlan 需要對應到**這張新網卡**建立
  獨立的 `192.168.89.0/24` docker network
- PC2 跟 PC3 兩個子網互相看不到彼此（各自只跟 PC1 直連），如果 Node6/7/8（PC3）需要
  跟 Node1~5（PC1/PC2）的 UE 有 UPF 層級的資料面互通，PC1 要在核心層面（Linux routing +
  iptables）手動搭橋兩個子網，這比現有的 CU DNAT 陷阱複雜得多——現有的 CU DNAT 問題
  只是「同一子網內的 IP 映射表沒同步」，方案 B 要處理的是「兩個不同子網之間的路由」，
  是完全不同量級的複雜度
- FlexRIC／Near-RT RIC 目前綁定在 `192.168.88.141`（PC1 的 macvlan IP），若 Node6/7/8
  在 `192.168.89.0/24` 子網，要嘛 FlexRIC 也要在新子網開一個對應介面，要嘛所有跨子網的
  E2 連線都要過 PC1 的路由層，等於又多一層可能出錯的環節
- 之後如果還要加第四台機器，這個「每加一台就多一張網卡、多一個子網、多一層路由」的
  模式會越來越難維護

**建議**：除非你完全沒有辦法取得一台 switch，否則方案 A 幾乎在所有面向都優於方案 B。
這不是效能考量，是「新增出錯環節的數量」考量——方案 B 會讓你在既有的 CU DNAT 陷阱之外，
再疊加一層子網路由陷阱，兩個問題同時存在時會很難排查是哪一層出錯。

---

## 2. IP／連接埠配置規劃（假設採用方案 A）

沿用現有 `192.168.88.0/24` 網段，避開已使用的位址：

| 用途 | 目前已用 | Node6/7/8 建議配置 |
|---|---|---|
| Donor/RIC | `.141` | 不動 |
| Node1/2 的 DU/gNB | `.131`–`.134`, `.144` | 不動 |
| Node1/2 本地 UE-side rfsim | `.150`, `.152` | 不動 |
| Node3/4/5 的 MT | `.30`/`.31`（Node3）、`.40`/`.41`（Node4）、`.50`/`.51`（Node5） | Node6/7/8 各自需要 2 個位址（比照現有 2-MT 的節點）：例如 `.60`/`.61`（Node6）、`.70`/`.71`（Node7）、`.80`/`.81`（Node8） |

IMSI 配置沿用現有的遞增規則（`208990100001100` 起算，Node3/4/5 用到
`208990100001102`~`208990100001104`），Node6/7/8 可接續用 `...1105`~`...1107`；
`rfsim5g-end-ue-N` 的 UE 編號目前用到 UE6，新增的 UE 接續為 UE7~UE12（Node6/7/8 各帶
2 個 UE，共 6 個新 UE，比照 CLAUDE.md 現有的「每 Node 兩個 MT/UE」模式）。

`rfsimulator.serverport` 目前 Node3 用 4044、Node4 用 4045，Node6/7/8 需要各自新的
獨立埠號（例如 4046/4047/4048，需要避開 Node1/2 本地 UE-side 可能已佔用的埠）。

---

## 3. 拓撲設計：Node6/7/8 掛在哪個 relay 下面

### 3.1 已確認：跟 Node3/4/5 一樣當 access node

不新增拓撲深度，只是拓寬（原本每個 relay 下面掛的 access node 變多）。

### 3.2 需要你決定：MT-to-relay 的對應規則

查了 `global_xapp.py` 的 `compute_quotas()`，發現目前的配額邏輯**寫死在「MT 數量」上**，
不是單純「幾個 access node」：

```python
# Node3 & Node4 配額：由 Node1 的兩個 MT（依 rnti 排序）各自的 PRB 分配決定
sorted_ues = sorted(node1_action, key=lambda u: u.get("rnti", 0))
q3 = sorted_ues[0]...   # 第一個 MT 的分配 → Node3 配額
q4 = sorted_ues[1]...   # 第二個 MT 的分配 → Node4 配額

# Node5 配額：Node2 唯一一個 MT 的分配「總和」
quotas[5] = sum(u.get("prb_abs", 0) for u in node2_action)
```

也就是說：**Node1 現在剛好有 2 個 MT，就精準對應 2 個 access node（Node3/4）；Node2
只有 1 個 MT，配額邏輯用「加總」而非「索引」處理**（因為 Node2 的 1 個 MT 服務不了
「依索引取第 N 個」這種邏輯）。這不是巧合，是刻意配平的 1:1（或 1:N 加總）設計。

新增 Node6/7/8 時，這個「MT 數量」的限制會逼你在兩條路線間二選一，我需要你決定要哪一條：

**路線 1：讓 Node1 或 Node2 多長出對應數量的 MT**（維持現有的 1:1 索引式邏輯，只是
擴充索引範圍到 3、4）
- 例如 Node1 從 2 個 MT 擴充成 4 個（服務 Node3/4/6/7），`compute_quotas()` 只要把
  `sorted_ues[0]/[1]` 擴充成 `[0]/[1]/[2]/[3]`，邏輯改動很小
- 但要在 Donor CU/DU 的 F1 設定、MT 容器定義裡讓 Node1 底下真的多長出 2 個 MT，這是
  RAN 層級的改動，牽涉到 gNB 的 F1AP 連線數量與 PRB 資源如何在多個 MT 之間切分
  （Node1 自己的 106 PRB 現在要餵給 4 個 MT，不是 2 個，PRB 供給的壓力會更緊繃）

**路線 2：改掉配額邏輯本身，從「依 MT 索引 1:1」改成「relay 總配額 ÷ access node 數量」**
- Node1（或 Node2）不用真的多長出新的 MT，`compute_quotas()` 改成看這個 relay 這一輪
  的**總 PRB 分配**，除以它服務的 access node 數量（可能是均分，也可能依某種比例）
- 改動範圍集中在 `global_xapp.py`／`global_xapp_bridge.py`，不用動到 RAN 底層的 MT
  數量與 F1 設定
- 語意上跟「回傳頻寬瓶頸」的物理意義更貼近（一個 relay 的總可用頻寬要分給它服務的
  所有下游 access node，不是「剛好一個 MT 對應一個 access node」這種巧合式的映射）

**我的建議是路線 2**：不動 RAN 層的 MT 數量（風險最低、不用改 Donor CU/DU 設定），
只改 Global 端的配額計算邏輯，而且路線 2 的語意其實更符合「IAB 回傳瓶頸模擬」的
原始設計動機（CLAUDE.md 第五階段開頭寫的「軟性回傳約束」），路線 1 的「一個 MT 對應
一個 access node」反而比較像是巧合出來的實作細節，不是刻意的設計。但這是會改變論文
裡「IAB 資源建模設計決策」敘事的選擇，需要你確認。

**分配方式（假設走路線 2）**：Node6/7 可以掛在 Node1 底下（讓 Node1 服務 Node3/4/6/7，
共 4 個），Node8 掛在 Node2 底下（讓 Node2 服務 Node5/8，共 2 個）——這樣兩邊 relay
服務的 access node 數量從現在的「2 vs 1」變成「4 vs 2」，維持大致相同的比例關係，不
是刻意選的，只是最小改動下的自然延伸；如果你有其他分配想法（例如全部 3 個新節點都掛
同一個 relay，測試單一 relay 的極限承載），也可以告訴我再調整。

---

## 4. 需要修改的檔案（分階段列出，比照專案既有的「由下而上」開發哲學）

### 階段 0：實體與底層連通性（比照第一階段「底層資料平面暢通」）

- 依 §1 選定方案完成實體接線
- PC3 安裝跟 PC1/PC2 相同版本的 Docker、OAI 建置環境（`cmake_targets/build_oai` 等）
- 確認 PC3 能 ping 通 PC1／PC2 的 macvlan IP

### 階段 1：C 語言層（比照第二、三階段）

- 新增 `openair2/E2AP/flexric/examples/xApp/c/ctrl/xapp_node6.c`（`xapp_node7.c`、
  `xapp_node8.c`），以 `xapp_node3.c`（同為 access node）為模板複製修改（node_id、
  ZMQ endpoint 等節點專屬常數）
- **依 CLAUDE.md §7 規範，每開發完一個 xApp，先編譯 FlexRIC xApp 和 OAI RAN with
  E2 Agent，編譯過了才能審核**——不能三個節點一次全部改完才測試，要一個一個來
- Donor CU/DU 或（若走路線 1）Node1/2 的 F1 設定需要能辨識新的 MT/DU 連線

### 階段 2：docker-compose 位址配置

- `docker-compose-iab-client.yaml`（若 PC3 沿用 PC2 現有的「client」角色定位）或新增
  `docker-compose-iab-pc3.yaml`：新增 Node6/7/8 的 MT/DU/UE 服務定義，依 §2 的 IP/
  IMSI/port 規劃
- `docker-compose-iab-server.yaml`：新增 `inference-node6/7/8`、`xapp-node6/7/8` 容器
  定義（若採路線 1，Node1/2 服務定義也要擴充 MT 數量）

### 階段 3：Python DRL／Global 層

- `inference/global_xapp.py`：`compute_quotas()` 依 §3.2 選定的路線修改
- `inference/global_xapp_bridge.py`：SUB/PUB 的節點清單擴充到 6/7/8
- `scenarios/traffic_scenario.py`：`NODE_CONFIG` 新增 Node6/7/8 對應的 UE 容器清單
- `inference/drl_agent.py`：`MAX_UE_COUNT`／`STATE_DIM` 如果新節點的 UE 數量不同於
  現有假設（目前每節點固定 2 個 UE，若延用不用改），需要重新確認
- **不需要改**：`drl_agent.py` 的網路架構本身（GRU/Actor/Critic）不因節點數量改變，
  每個節點依然是獨立的 `DRLAgent` 實例

### 階段 4：聯邦學習層

- `inference/flower-app/iab_fl/server_app.py`：`NUM_NODES` 從 5 改成 8，
  `min_train_nodes`／`min_evaluate_nodes`／`min_available_nodes` 同步調整（若已規劃
  分群 FedAvg，見 `DRL_METHODOLOGY_PLAN.md` 第二部分，這裡的節點清單也要同步擴充）
- `docker-compose-iab-server.yaml`：新增 `flower-supernode-node6/7/8` 服務定義

### 階段 5：啟動／監控腳本

- `iab/run_local_pc1.sh`、`iab/monitor_drl.sh`、`iab/start_iab_client.sh`：E2 連線數
  檢查從 `/6` 改成對應新的總數（Donor + 8 個 IAB node 的 E2 SETUP-REQUEST 數量，需要
  先確認現有的 6 這個數字實際構成——是 5 個 IAB node + Donor 本身 1 個，還是別的組合，
  新增後才能算出正確的新總數）
- 新增 `iab/run_local_pc3.sh`，比照 `run_local_pc2.sh` 的角色（等待 PC1 就緒、清空
  CU NAT table、啟動 UE benchmark 等）
- `iab/watchdog.sh`：目前只偵測 `xapp-node1` 的日誌判斷 E2 訂閱狀態，這個假設本身
  可能不需要改（Node1 是既有的哨兵節點），但要確認新節點加入後這個判斷邏輯依然有效

---

## 5. 建議的驗證順序

比照專案既有的「由下而上」六階段哲學，不要一次把三個新節點全部接上再測：

1. 先只接 Node6（單一新節點），走完整條路徑（C xApp 編譯 → E2 連線 → Python 推論
   → Global 配額 → MongoDB 經驗寫入），確認整條鏈路通了再進行下一個
2. Node6 穩定後，重複同樣流程做 Node7、Node8
3. 三個節點都個別驗證過後，才進行「PC3 全部一起啟動」的整合測試
4. 全部穩定後，才把 Node6/7/8 納入 Flower 聯邦學習的聚合範圍（階段 4）

這個順序刻意比 CLAUDE.md 記錄的「Node1~5 各自獨立開發」模式更保守一步——因為這次
新增的是全新一台實體主機，物理連接本身就是一個新的失敗風險來源，跟純軟體層面的
xApp 開發不是同一個量級的風險。

---

## 6. 本次執行範圍

**這份文件本身只是規劃，尚未開始任何實作**。下一步：

1. 先決定 §1 的物理連接方案（switch 還是第二張網卡）——需要你評估手邊資源後回覆
2. 再決定 §3.2 的 MT-to-relay 對應路線（路線 1 或路線 2）——這會實質影響論文裡
   「IAB 資源建模」的敘事，建議你自己判斷後回覆
3. 兩個決定都確認後，我會先進 Plan 模式，把階段 1（第一個新節點 Node6 的 C xApp
   開發）拆成具體的逐檔案改動計劃，依 CLAUDE.md 規範走「開發一個、編譯一個、審核
   一個」的節奏，不會一次把三個節點的程式碼全部生成出來
