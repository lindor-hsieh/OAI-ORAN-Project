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
#include "openair2/LAYER2/nr_rlc/nr_rlc_oai_api.h"
#include "openair2/E2AP/flexric/src/sm/mac_sm/ie/mac_data_ie.h" 

#include <assert.h>
#include <time.h>
#include <stdio.h>
#include <stdlib.h>
#include <pthread.h>

static const int mod_id = 0;

// ==========================================
// 1. 感知層：讀取 MAC/RLC 統計數據 (Indication)
// ==========================================
bool read_mac_sm(void* data)
{
  assert(data != NULL);
  mac_ind_data_t* mac = (mac_ind_data_t*)data;
  
  // [Fix 1] 確保時間戳記被正確寫入
  struct timespec ts;
  clock_gettime(CLOCK_REALTIME, &ts); // 改用 REALTIME 確保與系統時間一致
  mac->msg.tstamp = (int64_t)ts.tv_sec * 1000000 + ts.tv_nsec / 1000;

  if (RC.nrmac == NULL || RC.nrmac[mod_id] == NULL) {
      printf("[E2-AGENT] Error: RC.nrmac is NULL!\n");
      return false;
  }

  gNB_MAC_INST *nrmac = RC.nrmac[mod_id];
  NR_UEs_t *UE_info = &nrmac->UE_info;

  // -------------------------------------------------------------
  // [Fix 2] 放棄 UE_iterator 巨集，改用暴力陣列遍歷
  // -------------------------------------------------------------
  
  // A. 第一次遍歷：計算 UE 數量
  size_t num_ues = 0;
  for (int i = 0; i < MAX_MOBILES_PER_GNB; i++) {
      if (UE_info->connected_ue_list[i] != NULL) {
          num_ues++;
      }
  }

  // [Debug] 在 gNB 終端機印出偵測到的數量，確認 Agent 是否活著
  if (num_ues > 0) {
      // printf("[E2-AGENT] read_mac_sm detected %zu UEs.\n", num_ues);
  }

  mac->msg.len_ue_stats = num_ues;
  
  // 如果沒有 UE，直接返回，不要分配記憶體 (避免 malloc(0) 行為不一致)
  if (num_ues == 0) {
      mac->msg.ue_stats = NULL;
      return true;
  }

  // 分配記憶體
  mac->msg.ue_stats = calloc(num_ues, sizeof(mac_ue_stats_impl_t));
  assert(mac->msg.ue_stats != NULL && "Memory exhausted");

  // B. 第二次遍歷：填入數據
  size_t idx = 0;
  for (int i = 0; i < MAX_MOBILES_PER_GNB; i++) {
      NR_UE_info_t* UE = UE_info->connected_ue_list[i];
      
      if (UE != NULL) {
          // 確保不越界
          if (idx >= num_ues) break;

          mac_ue_stats_impl_t* rd = &mac->msg.ue_stats[idx];
          NR_UE_sched_ctrl_t* sched_ctrl = &UE->UE_sched_ctrl;

          // 基礎資訊
          rd->frame = nrmac->frame;
          rd->slot = 0;
          rd->rnti = UE->rnti;

          // 論文關鍵指標
          rd->dl_buffer_info = (uint32_t)sched_ctrl->num_total_bytes;
          rd->ul_buffer_info = (uint32_t)sched_ctrl->estimated_ul_buffer;
          rd->rlc_delay_ms = 0.0; // 暫無計算

          // 品質指標
          rd->wb_cqi = (uint8_t)sched_ctrl->CSI_report.cri_ri_li_pmi_cqi_report.wb_cqi_1tb;
          rd->dl_mcs1 = (uint8_t)sched_ctrl->dl_bler_stats.mcs;
          rd->dl_bler = (float)sched_ctrl->dl_bler_stats.bler;

          // 流量統計
          rd->dl_aggr_tbs = UE->mac_stats.dl.total_bytes;
          rd->ul_aggr_tbs = UE->mac_stats.ul.total_bytes;
          rd->dl_aggr_prb = UE->mac_stats.dl.total_rbs;
          rd->ul_aggr_prb = UE->mac_stats.ul.total_rbs;

          idx++;
      }
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
  sm_ag_if_ans_t ans = {0};

  // [Debug]
  printf("[OAI-E2-AGENT] Recv Control: Type=%d, Slices=%d\n", 
         req->msg.type, req->msg.len_slices);

  if (req->msg.len_slices > 0) {
      if (RC.nrmac && RC.nrmac[mod_id]) {
          gNB_MAC_INST *nrmac = RC.nrmac[mod_id];

          for (size_t i = 0; i < req->msg.len_slices; i++) {
              uint16_t rnti = (uint16_t)req->msg.slices[i].id;
              float prb_quota = req->msg.slices[i].prb_quota;
              uint16_t slot_mask = req->msg.slices[i].slot_mask;

              // 存入 gNB 結構體，供排程器使用
              if (i == 0) {
                  nrmac->xapp_2d_ctrl.rnti1 = rnti;
                  nrmac->xapp_2d_ctrl.prb_ratio1 = prb_quota;
                  nrmac->xapp_2d_ctrl.slot_mask1 = slot_mask;
              } else if (i == 1) {
                  nrmac->xapp_2d_ctrl.rnti2 = rnti;
                  nrmac->xapp_2d_ctrl.prb_ratio2 = prb_quota;
                  nrmac->xapp_2d_ctrl.slot_mask2 = slot_mask;
              }
          }
          printf("[OAI-E2-AGENT] >>> Applied: UE1(%04x) Ratio:%.2f Mask:%04x <<<\n", 
                 nrmac->xapp_2d_ctrl.rnti1, nrmac->xapp_2d_ctrl.prb_ratio1, nrmac->xapp_2d_ctrl.slot_mask1);
      }
  } 
  
  return ans;
}