為指定 Node 建立或修改 Local xApp。

**參數**：Node 編號（1~5），例如 `/project:xapp-new 3`

**開發規範**：
- xApp 主程式：`~/openairinterface5g/openair2/E2AP/flexric/examples/xApp/c/ctrl/mac_ctrl_node<N>.c`
- 共用基礎設施（所有 node 共用，不可各自修改）：
  - `src/sm/mac_sm/mac_data_ie.c` / `.h`
  - `src/sm/mac_sm/mac_enc_plain.c` / `mac_dec_plain.c`
  - `src/sm/mac_sm/mac_sm_agent.c` / `mac_sm_ric.c`
  - `src/xApp/sm_ran_function_def.c`
  - `openair2/E2AP/RAN_FUNCTION/ran_func_mac.c`
  - `openair2/LAYER2/NR_MAC_gNB/nr_mac_gNB.h`
  - `openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_dlsch.c`

**每個 xApp 必須包含**：
1. ZeroMQ REQ socket，連接對應 Python 推論伺服器（port 依 node 區分，Node 1=5555, 2=5556, 3=5557, 4=5558, 5=5559）
2. 5ms timeout + fallback（退回 OAI 預設排程）
3. cJSON 解析 BSR/CQI，送出後呼叫 `cJSON_Delete`
4. `[Local xApp Node<N>]` 前綴的日誌

完成後提醒用 `/project:build-xapp` 編譯驗證。
