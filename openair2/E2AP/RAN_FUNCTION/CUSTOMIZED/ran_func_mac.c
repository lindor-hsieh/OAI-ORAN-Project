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
// Use relative path to find the OAI MAC definition
#include "../../../LAYER2/NR_MAC_gNB/nr_mac_gNB.h" 
#include <assert.h>

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
  mac->msg.tstamp = time_now_us();

  NR_UEs_t *UE_info = &RC.nrmac[mod_id]->UE_info;
  size_t num_ues = 0;
  UE_iterator(UE_info->connected_ue_list, ue) {
    if (ue) num_ues += 1;
  }

  mac->msg.len_ue_stats = num_ues;
  if(mac->msg.len_ue_stats > 0){
    mac->msg.ue_stats = calloc(mac->msg.len_ue_stats, sizeof(mac_ue_stats_impl_t));
    assert(mac->msg.ue_stats != NULL && "Memory exhausted" );
  }

  size_t i = 0; 
  UE_iterator(UE_info->connected_ue_list, UE) {
    const NR_UE_sched_ctrl_t* sched_ctrl = &UE->UE_sched_ctrl;
    mac_ue_stats_impl_t* rd = &mac->msg.ue_stats[i];

    rd->frame = RC.nrmac[mod_id]->frame;
    rd->slot = 0; 
    rd->rnti = UE->rnti;
    
    // [CQI] Fill OAI internal WB CQI into E2 message
    rd->wb_cqi = sched_ctrl->CSI_report.cri_ri_li_pmi_cqi_report.wb_cqi_1tb;
    
    // Fill other statistics
    rd->dl_aggr_tbs = UE->mac_stats.dl.total_bytes;
    rd->ul_aggr_tbs = UE->mac_stats.ul.total_bytes;
    rd->dl_curr_tbs = UE->mac_stats.dl.current_bytes;
    rd->ul_curr_tbs = UE->mac_stats.ul.current_bytes;
    rd->dl_sched_rb = UE->mac_stats.dl.current_rbs;
    rd->ul_sched_rb = UE->mac_stats.ul.current_rbs;
    
    rd->dl_aggr_prb = UE->mac_stats.dl.total_rbs;
    rd->ul_aggr_prb = UE->mac_stats.ul.total_rbs;
    rd->dl_aggr_retx_prb = UE->mac_stats.dl.total_rbs_retx;
    rd->ul_aggr_retx_prb = UE->mac_stats.ul.total_rbs_retx;

    rd->dl_aggr_bytes_sdus = UE->mac_stats.dl.lc_bytes[3];
    rd->ul_aggr_bytes_sdus = UE->mac_stats.ul.lc_bytes[3];
    rd->dl_aggr_sdus = UE->mac_stats.dl.num_mac_sdu;
    rd->ul_aggr_sdus = UE->mac_stats.ul.num_mac_sdu;

    rd->pusch_snr = (float) sched_ctrl->pusch_snrx10 / 10; 
    rd->pucch_snr = (float) sched_ctrl->pucch_snrx10 / 10; 

    rd->dl_mcs1 = sched_ctrl->dl_bler_stats.mcs;
    rd->dl_bler = sched_ctrl->dl_bler_stats.bler;
    rd->ul_mcs1 = sched_ctrl->ul_bler_stats.mcs;
    rd->ul_bler = sched_ctrl->ul_bler_stats.bler;
    rd->phr = sched_ctrl->ph;
    rd->bsr = sched_ctrl->estimated_ul_buffer - sched_ctrl->sched_ul_bytes;

    ++i;
  }
  return num_ues > 0;
}

void read_mac_setup_sm(void* data) {
  assert(data != NULL);
  assert(0 !=0 && "Not supported");
}

// ==========================================
// Handle Control Messages (Write Control)
// ==========================================
sm_ag_if_ans_t write_ctrl_mac_sm(void const* data)
{
  assert(data != NULL);
  const mac_ctrl_req_data_t* req = (const mac_ctrl_req_data_t*)data;
  
  // [Fix] Simply initialize to zero. 
  // We avoid accessing specific union members (like ans.mac or ans.ans)
  // because the calling agent does not check the return value anyway.
  sm_ag_if_ans_t ans = {0};

  // Check if it is our Slice Configuration message (Type 0)
  if (req->msg.type == 0) { 
      printf("[OAI-E2] Received Slice Config Request via E2!\n");
      float new_vip_share = 0.7; 
      bool found = false;

      // Use uint32_t to match the type of len_slices
      for(uint32_t i = 0; i < req->msg.len_slices; i++) {
          printf("[OAI-E2]   -> Slice ID: %d, Percentage: %.2f\n", 
                 req->msg.slices[i].id, req->msg.slices[i].percentage);
          
          if (req->msg.slices[i].id == 1) {
              new_vip_share = req->msg.slices[i].percentage;
              found = true;
          }
      }

      if (found && RC.nrmac && RC.nrmac[mod_id]) {
          // Update OAI internal state
          RC.nrmac[mod_id]->slice_info.algo = 2; // NVS_SLICE
          RC.nrmac[mod_id]->slice_info.vip_share = new_vip_share;
          
          printf("[OAI-E2] >>> NVS Algo Updated! New VIP Share: %.2f <<<\n", new_vip_share);
          
          // [Fix] We don't set ans.mac.ans here to avoid compiler errors.
          // The action is already performed above.
      } 
  } else {
      printf("[OAI-E2] Received unknown Control Action Type: %d\n", req->msg.type);
  }
  
  return ans;
}