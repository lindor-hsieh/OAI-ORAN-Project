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

#ifndef TELNETSRV_BHLOAD_H
#define TELNETSRV_BHLOAD_H

/* [Backhaul-aware PRB budget] 註冊 "bhload query" telnet 命令，只能在 nr-uesoftmodem
 * (MT/UE) process 呼叫，不能在 nr-softmodem (DU/gNB) process 呼叫。 */
void init_bhload_telnetcmd(void);

#endif /* TELNETSRV_BHLOAD_H */
