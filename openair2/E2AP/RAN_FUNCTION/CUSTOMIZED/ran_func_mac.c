/*
 * Licensed to the OpenAirInterface (OAI) Software Alliance under one or more
 * contributor license agreements.  See the NOTICE file distributed with
 * this work for additional information regarding copyright ownership.
 * The OpenAirInterface Software Alliance licenses this file to You under
 * the OAI Public License, Version 1.1  (the "License"); you may not use this file
 * except in compliance with the License.
 * You may obtain a copy of the License at
 *
 * http://www.openairinterface.org/?page_id=698
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *-------------------------------------------------------------------------------
 * For more information about the OpenAirInterface (OAI) Software Alliance:
 * contact@openairinterface.org
 */

#include "ran_func_mac.h"
#include "openair2/LAYER2/NR_MAC_gNB/nr_mac_gNB.h"
#include "openair2/LAYER2/nr_rlc/nr_rlc_oai_api.h" // 關鍵：用於讀取 RLC Buffer
#include "openair2/E2AP/flexric/src/sm/mac_sm/ie/mac_data_ie.h" // 引用我們修改過的 IE 定義

#include <assert.h>
#include <time.h>
#include <stdio.h>
#include <stdlib.h>

static const int mod_id = 0;

// ====================================================================
// [Global Control Variables]
// 這些變數是用來連接 E2 Agent (這裡) 與 MAC Scheduler (gNB_scheduler_dlsch.c) 的橋樑。
// 未來無論是 Node 3, 4, 5，只要編譯這份 code，它們都會具備接收指令的能力。
// ====================================================================
uint16_t target_rnti_1 = 0;
float target_ue1_prb_ratio = 1.0;
uint16_t target_ue1_slot_mask = 0xFFFF;

uint16_t target_rnti_2 = 0;
float target_ue2_prb_ratio = 1.0;
uint16_t target_ue2_slot_mask = 0xFFFF;
// ====================================================================

// ==========================================
// 1. 感知層：讀取 MAC/RLC 統計數據 (Indication)
// ==========================================
bool read_mac_sm(void* data)
{
  assert(data != NULL);
  mac_ind_data_t* mac = (mac_ind_data_t*)data;
  
  // 使用標準時間戳計 (us)
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  mac->msg.tstamp = (int64_t)ts.tv_sec * 1000000 + ts.tv_nsec / 1000;

  if (RC.nrmac == NULL || RC.nrmac[mod_id] == NULL) return false;

  NR_UEs_t *UE_info = &RC.nrmac[mod_id]->UE_info;
  
  // A. 統計當前連線的 UE 數量
  size_t num_ues = 0;
  UE_iterator(UE_info->connected_ue_list, ue) {
    if (ue) num_ues += 1;
  }

  mac->msg.len_ue_stats = num_ues;
  if(mac->msg.len_ue_stats > 0){
    mac->msg.ue_stats = calloc(mac->msg.len_ue_stats, sizeof(mac_ue_stats_impl_t));
    assert(mac->msg.ue_stats != NULL && "Memory exhausted" );
  } else {
    return true; // 無連線 UE 則回報空訊息
  }

  // B. 填入每個 UE 的即時數據
  size_t ue_idx = 0; 
  UE_iterator(UE_info->connected_ue_list, UE) {
    const NR_UE_sched_ctrl_t* sched_ctrl = &UE->UE_sched_ctrl;
    mac_ue_stats_impl_t* rd = &mac->msg.ue_stats[ue_idx];

    // --- 基礎欄位 ---
    rd->frame = RC.nrmac[mod_id]->frame;
    rd->slot = 0; 
    rd->rnti = UE->rnti;
    
    // --- [論文核心數據]：即時 Buffer 狀態與延遲 ---
    // 透過 RLC API 抓取特定 UE 的下行緩衝區大小 (bytes)
    // 這裡假設我們要抓取 DRB (Data Radio Bearer)，通常 LCID 從 4 開始
    // 簡單起見，我們加總所有 LCID 的 buffer
    rd->dl_buffer_info = 0;
    for (int lcid = 1; lcid < 8; lcid++) {
        rd->dl_buffer_info += (uint32_t)nr_rlc_get_available_transmit_buffer_size(UE->rnti, lcid, 1);
    }
    
    rd->ul_buffer_info = (uint32_t)UE->mac_stats.ul.bsr; // 上行參考 BSR
    
    // 抓取 RLC 層觀測到的平均延遲 (若 OAI 有統計的話，或是用 buffer 推估)
    rd->rlc_delay_ms = (float)UE->mac_stats.dl.lc_bytes[3] > 0 ? 
                       (float)UE->mac_stats.dl.lc_bytes[3] / 1000.0 : 0.0;

    // --- 實驗判斷核心 (MCS, CQI, BLER) ---
    rd->wb_cqi = (uint8_t)sched_ctrl->CSI_report.cri_ri_li_pmi_cqi_report.wb_cqi_1tb;
    rd->dl_mcs1 = (uint8_t)sched_ctrl->dl_bler_stats.mcs; 
    rd->dl_bler = (float)sched_ctrl->dl_bler_stats.bler;
    
    // --- 累積流量統計 ---
    rd->dl_aggr_tbs = UE->mac_stats.dl.total_bytes;
    rd->ul_aggr_tbs = UE->mac_stats.ul.total_bytes;
    rd->dl_aggr_prb = UE->mac_stats.dl.total_rbs;
    rd->ul_aggr_prb = UE->mac_stats.ul.total_rbs;

    ue_idx++;
  }

  return true;
}

void read_mac_setup_sm(void* data) {
  (void)data;
}

// ==========================================
// 2. 控制層：處理來自 Local xApp 的資源分配指令 (Control)
// ==========================================
sm_ag_if_ans_t write_ctrl_mac_sm(void const* data)
{
  assert(data != NULL);
  const mac_ctrl_req_data_t* req = (const mac_ctrl_req_data_t*)data;

  // 檢查指令類型 (Type 0 為資源分配) 與切片數量
  if (req->msg.type == 0 && req->msg.len_slices >= 2) { 
      
      // [通用設計] 
      // 這裡我們假設 xApp 總是送來兩個切片的設定 (VIP vs Standard)
      // 如果你要支援更多 UE，可以用迴圈動態更新陣列，但目前 2 個變數最快最穩。

      // --- 提取 UE 1 (VIP) 的設定 ---
      target_rnti_1 = (uint16_t)req->msg.slices[0].id;
      target_ue1_prb_ratio = req->msg.slices[0].prb_quota;
      target_ue1_slot_mask = req->msg.slices[0].slot_mask; // [新增] 更新 Slot Mask
      
      // --- 提取 UE 2 (Standard) 的設定 ---
      target_rnti_2 = (uint16_t)req->msg.slices[1].id;
      target_ue2_prb_ratio = req->msg.slices[1].prb_quota;
      target_ue2_slot_mask = req->msg.slices[1].slot_mask; // [新增] 更新 Slot Mask

      printf("[OAI-E2-AGENT] >> 收到 2D 控制指令!\n");
      printf("   UE1(%04x) -> PRB: %.2f | SlotMask: %04X\n", 
             target_rnti_1, target_ue1_prb_ratio, target_ue1_slot_mask);
      printf("   UE2(%04x) -> PRB: %.2f | SlotMask: %04X\n", 
             target_rnti_2, target_ue2_prb_ratio, target_ue2_slot_mask);

  } else {
      printf("[OAI-E2-AGENT] 收到未知的控制類型或切片數量不足 (Expected 2, got %d)\n", req->msg.len_slices);
  }
  
  sm_ag_if_ans_t ans = {0};
  return ans;
}