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

/*
 * [Backhaul-aware PRB budget] MT 端 telnetsrv 模組："bhload get"
 *
 * 只在 nr-uesoftmodem（MT/UE）process 裡註冊，暴露自己的累積 DL/UL RB 使用量
 * （NR_UE_MAC_INST_t.stats.dl/ul.rb_size），給同一個 container/netns 內、
 * 自己的 DU（relay 節點的 nr-softmodem）透過輪詢讀取，藉此推算「這個節點的
 * backhaul 現在有多忙」，動態縮小 DU 這次排程週期可用的 PRB 池。
 *
 * 刻意保持 MT 端無狀態：只回傳原始累積值，delta/比例運算留給 DU 端做。
 * 註冊模式完全比照既有的 channelmod telnet 命令（見 random_channel.c）。
 */

#include <stdio.h>
#include "common/utils/telnetsrv/telnetsrv.h"
#include "common/utils/load_module_shlib.h"
#include "openair2/LAYER2/NR_MAC_UE/mac_proto.h"
#include "openair2/LAYER2/NR_MAC_UE/mac_defs.h"

static int bhload_get_cmd(char *buff, int debug, telnet_printfunc_t prnt)
{
  NR_UE_MAC_INST_t *mac = get_mac_inst(0);
  if (mac == NULL) {
    prnt("bhload: mac instance not ready\n");
    return 0;
  }
  prnt("dl_rb_cum %lu ul_rb_cum %lu\n",
       (unsigned long)mac->stats.dl.rb_size,
       (unsigned long)mac->stats.ul.rb_size);
  return 0;
}

static telnetshell_cmddef_t bhload_cmdarray[] = {
    {"get", "", bhload_get_cmd, {NULL}, TELNETSRV_CMDFLAG_TELNETONLY, NULL},
    {"", "", NULL, {NULL}, 0, NULL},
};

static telnetshell_vardef_t bhload_vardef[] = {{"", 0, 0, NULL}};

void init_bhload_telnetcmd(void)
{
  /* look for telnet server, if it is loaded, add the bhload command to it */
  add_telnetcmd_func_t addcmd = (add_telnetcmd_func_t)get_shlibmodule_fptr("telnetsrv", TELNET_ADDCMD_FNAME);

  if (addcmd != NULL) {
    addcmd("bhload", bhload_vardef, bhload_cmdarray);
  }
} /* init_bhload_telnetcmd */
