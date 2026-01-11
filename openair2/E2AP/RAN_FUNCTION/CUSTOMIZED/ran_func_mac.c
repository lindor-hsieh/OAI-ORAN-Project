/*
 * Licensed to the OpenAirInterface (OAI) Software Alliance ...
 */

#include "ran_func_mac.h"
#include "../../../LAYER2/NR_MAC_gNB/nr_mac_gNB.h" 
#include <assert.h>
#include <time.h>
#include <stdio.h>
#include <stdlib.h>

// [IMPORTANT] Point to the FlexRIC IE definition. 
#include "/home/lindor/openairinterface5g/openair2/E2AP/flexric/src/sm/mac_sm/ie/mac_data_ie.h"

static const int mod_id = 0;

// ==========================================
// Read MAC Statistics (Indication)
// ==========================================
bool read_mac_sm(void* data)
{
  assert(data != NULL);
  mac_ind_data_t* mac = (mac_ind_data_t*)data;
  
  // 使用標準時間戳計，解決 time_now_us 未定義問題
  mac->msg.tstamp = (int64_t)time(NULL) * 1000000;

  if (RC.nrmac == NULL || RC.nrmac[mod_id] == NULL) return false;

  NR_UEs_t *UE_info = &RC.nrmac[mod_id]->UE_info;
  
  // A. 統計當前連線的 UE 數量 - 改用手動遍歷避開 UE_iterator 巨集錯誤
  size_t num_ues = 0;
  for (int i = 0; i < MAX_MOBILES_PER_GNB; i++) {
    if (UE_info->connected_ue_list[i] != NULL) {
        num_ues++;
    }
  }

  mac->msg.len_ue_stats = num_ues;
  if(mac->msg.len_ue_stats > 0){
    mac->msg.ue_stats = calloc(mac->msg.len_ue_stats, sizeof(mac_ue_stats_impl_t));
    assert(mac->msg.ue_stats != NULL && "Memory exhausted" );
  } else {
    return true; 
  }

  // B. 填入每個 UE 的即時數據
  size_t ue_idx = 0; 
  for (int i = 0; i < MAX_MOBILES_PER_GNB; i++) {
    NR_UE_info_t *UE = UE_info->connected_ue_list[i];
    if (UE == NULL) continue;

    const NR_UE_sched_ctrl_t* sched_ctrl = &UE->UE_sched_ctrl;
    mac_ue_stats_impl_t* rd = &mac->msg.ue_stats[ue_idx];

    rd->frame = RC.nrmac[mod_id]->frame;
    rd->slot = 0; 
    rd->rnti = UE->rnti;
    
    // [關鍵數據] Fill OAI internal MCS and CQI into E2 message
    rd->wb_cqi = (uint8_t)sched_ctrl->CSI_report.cri_ri_li_pmi_cqi_report.wb_cqi_1tb;
    rd->dl_mcs1 = (uint8_t)sched_ctrl->dl_bler_stats.mcs; // 這是實驗判斷的核心
    rd->dl_bler = (float)sched_ctrl->dl_bler_stats.bler;
    
    // Fill other statistics
    rd->dl_aggr_tbs = UE->mac_stats.dl.total_bytes;
    rd->ul_aggr_tbs = UE->mac_stats.ul.total_bytes;
    rd->dl_aggr_prb = UE->mac_stats.dl.total_rbs;
    rd->ul_aggr_prb = UE->mac_stats.ul.total_rbs;

    ue_idx++;
    if (ue_idx >= num_ues) break;
  }

  printf("[OAI-E2] >>> Reporting %zu UEs with dynamic MCS to RIC <<<\n", num_ues);
  return true;
}

void read_mac_setup_sm(void* data) {
  (void)data;
}

// ==========================================
// Handle Control Messages (Write Control)
// ==========================================
sm_ag_if_ans_t write_ctrl_mac_sm(void const* data)
{
  assert(data != NULL);
  const mac_ctrl_req_data_t* req = (const mac_ctrl_req_data_t*)data;
  sm_ag_if_ans_t ans = {0};

  // Check if it is our Slice Configuration message (Type 0)
  if (req->msg.type == 0) { 
      printf("[OAI-E2] Received Slice Config Request via E2!\n");
      
      if (RC.nrmac && RC.nrmac[mod_id]) {
          gNB_MAC_INST *nrmac = RC.nrmac[mod_id];
          nrmac->slice_info.algo = 2; // 開啟 NVS
          nrmac->slice_info.n_slices = req->msg.len_slices;

          for(uint32_t i = 0; i < req->msg.len_slices; i++) {
              // 更新到 gNB 的陣列中
              nrmac->slice_info.slices[i].id = req->msg.slices[i].id;
              nrmac->slice_info.slices[i].percentage = req->msg.slices[i].percentage;
              
              if (req->msg.slices[i].id == 1) { // VIP Slice
                  nrmac->slice_info.vip_share = req->msg.slices[i].percentage;
                  printf("[OAI-E2] >>> Updated VIP Share: %.2f <<<\n", req->msg.slices[i].percentage);
              }
          }
      } 
  }
  
  return ans;
}