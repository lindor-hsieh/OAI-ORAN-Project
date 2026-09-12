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

#ifndef NR_MAC_GNB_BACKHAUL_POLL_H
#define NR_MAC_GNB_BACKHAUL_POLL_H

#include "nr_mac_gNB.h"

/* [Backhaul-aware PRB budget] 啟動一個獨立輪詢執行緒，定期連到本節點自己 MT 的
 * telnetsrv port（"bhload get"），推算 backhaul 忙碌程度並寫入 mac->backhaul_prb_ratio。
 * mt_telnet_port <= 0 時（例如 Donor 沒有 MT）直接不啟動，ratio 恆為初始值 1.0（無約束）。
 * mt_telnet_addr：relay 節點 MT/DU 共用 netns 用 "127.0.0.1"；access 節點 MT/DU 是
 * 分開的 netns（透過 iab_internal_net bridge 連接），要填 MT 在該 bridge 上的 IP。
 */
void start_backhaul_poll_thread(gNB_MAC_INST *mac, int mt_telnet_port, const char *mt_telnet_addr);

#endif /* NR_MAC_GNB_BACKHAUL_POLL_H */
