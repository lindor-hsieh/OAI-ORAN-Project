/*
 * Licensed to the OpenAirInterface (OAI) Software Alliance under one or more
 * contributor license agreements.  See the NOTICE file distributed with
 * this work for additional information regarding copyright ownership.
 * The OpenAirInterface Software Alliance licenses this file to You under
 * the OAI Public License, Version 1.1  (the "License"); you may not use this file
 * except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      http://www.openairinterface.org/?page_id=698
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *-------------------------------------------------------------------------------
 * For more information about the OpenAirInterface (OAI) Software Alliance:
 *      contact@openairinterface.org
 */

#include "ran_func_mac.h"
#include "/home/lindor/openairinterface5g/openair2/E2AP/flexric/src/sm/mac_sm/ie/mac_data_ie.h"
#include <assert.h>

static
const int mod_id = 0;


bool read_mac_sm(void* data)
{
  assert(data != NULL);

  mac_ind_data_t* mac = (mac_ind_data_t*)data;
  //fill_mac_ind_data(mac);

  mac->msg.tstamp = time_now_us();

  NR_UEs_t *UE_info = &RC.nrmac[mod_id]->UE_info;
  size_t num_ues = 0;
  UE_iterator(UE_info->connected_ue_list, ue) {
    if (ue)
      num_ues += 1;
  }

  mac->msg.len_ue_stats = num_ues;
  if(mac->msg.len_ue_stats > 0){
    mac->msg.ue_stats = calloc(mac->msg.len_ue_stats, sizeof(mac_ue_stats_impl_t));
    assert(mac->msg.ue_stats != NULL && "Memory exhausted" );
  }

  size_t i = 0; //TODO
  UE_iterator(UE_info->connected_ue_list, UE) {
    const NR_UE_sched_ctrl_t* sched_ctrl = &UE->UE_sched_ctrl;
    mac_ue_stats_impl_t* rd = &mac->msg.ue_stats[i];

    rd->frame = RC.nrmac[mod_id]->frame;
    rd->slot = 0; // previously had slot info, but the gNB runs multiple slots
                  // in parallel, so this has no real meaning

    rd->dl_aggr_tbs = UE->mac_stats.dl.total_bytes;
    rd->ul_aggr_tbs = UE->mac_stats.ul.total_bytes;

    if (is_dl_slot(rd->slot, &RC.nrmac[mod_id]->frame_structure)) {
      rd->dl_curr_tbs = UE->mac_stats.dl.current_bytes;
      rd->dl_sched_rb = UE->mac_stats.dl.current_rbs;
    }
    if (is_ul_slot(rd->slot, &RC.nrmac[mod_id]->frame_structure)) {
      rd->ul_curr_tbs = UE->mac_stats.ul.current_bytes;
      rd->ul_sched_rb = UE->mac_stats.ul.current_rbs;
    }

    rd->rnti = UE->rnti;
    rd->dl_aggr_prb = UE->mac_stats.dl.total_rbs;
    rd->ul_aggr_prb = UE->mac_stats.ul.total_rbs;
    rd->dl_aggr_retx_prb = UE->mac_stats.dl.total_rbs_retx;
    rd->ul_aggr_retx_prb = UE->mac_stats.ul.total_rbs_retx;

    rd->dl_aggr_bytes_sdus = UE->mac_stats.dl.lc_bytes[3];
    rd->ul_aggr_bytes_sdus = UE->mac_stats.ul.lc_bytes[3];

    rd->dl_aggr_sdus = UE->mac_stats.dl.num_mac_sdu;
    rd->ul_aggr_sdus = UE->mac_stats.ul.num_mac_sdu;

    rd->pusch_snr = (float) sched_ctrl->pusch_snrx10 / 10; //: float = -64;
    rd->pucch_snr = (float) sched_ctrl->pucch_snrx10 / 10; //: float = -64;

    rd->wb_cqi = sched_ctrl->CSI_report.cri_ri_li_pmi_cqi_report.wb_cqi_1tb;
    rd->dl_mcs1 = sched_ctrl->dl_bler_stats.mcs;
    rd->dl_bler = sched_ctrl->dl_bler_stats.bler;
    rd->ul_mcs1 = sched_ctrl->ul_bler_stats.mcs;
    rd->ul_bler = sched_ctrl->ul_bler_stats.bler;
    rd->dl_mcs2 = 0;
    rd->ul_mcs2 = 0;
    rd->phr = sched_ctrl->ph;

    const uint32_t bufferSize = sched_ctrl->estimated_ul_buffer - sched_ctrl->sched_ul_bytes;
    rd->bsr = bufferSize;

    const size_t numDLHarq = 4;
    rd->dl_num_harq = numDLHarq;
    for (uint8_t j = 0; j < numDLHarq; ++j)
      rd->dl_harq[j] = UE->mac_stats.dl.rounds[j];
    rd->dl_harq[numDLHarq] = UE->mac_stats.dl.errors;

    const size_t numUlHarq = 4;
    rd->ul_num_harq = numUlHarq;
    for (uint8_t j = 0; j < numUlHarq; ++j)
      rd->ul_harq[j] = UE->mac_stats.ul.rounds[j];
    rd->ul_harq[numUlHarq] = UE->mac_stats.ul.errors;

    ++i;
  }

  return num_ues > 0;
}

void read_mac_setup_sm(void* data)
{
  assert(data != NULL);
  assert(0 !=0 && "Not supported");
}

sm_ag_if_ans_t write_ctrl_mac_sm(void const* data)
{
  assert(data != NULL);
  
  // 1. 轉型：把通用指標轉回我們的控制請求結構
  const mac_ctrl_req_data_t* req = (const mac_ctrl_req_data_t*)data;

  // 2. 處理切片設定 (Type 0)
  if (req->msg.type == 0) { // MAC_CTRL_REQ_DL_SLICE_CONF
      printf("[OAI-E2] Received Slice Config Request!\n");

      // 印出收到的參數 (驗證用)
      for(int i = 0; i < req->msg.len_slices; i++) {
          printf("   - Slice ID: %d, Percentage: %.2f\n", 
                 req->msg.slices[i].id, 
                 req->msg.slices[i].percentage);
      }

      // 3. [關鍵] 啟動 gNB 的 NVS 模式
      if (RC.nrmac && RC.nrmac[mod_id]) {
          // 這裡就是開啟你 gNB_scheduler_dlsch.c 邏輯的開關
          RC.nrmac[mod_id]->slice_info.algo = 2; // NVS_SLICE
          
          printf("[OAI-E2] >>> NVS Slicing Mode ACTIVATED! <<<\n");
      }
  } else {
      printf("[OAI-E2] Received unknown control type: %d\n", req->msg.type);
  }

  // 回傳成功狀態
  sm_ag_if_ans_t ans = {0};
  return ans;
}

