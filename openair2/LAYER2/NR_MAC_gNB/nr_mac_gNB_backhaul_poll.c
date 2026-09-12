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
 * [Backhaul-aware PRB budget] DU 端輪詢執行緒。
 *
 * relay/access 節點的 DU（本 process）跟自己的 MT 共用同一個 Docker network
 * namespace，兩者是不同的 process（nr-softmodem vs nr-uesoftmodem）。這支執行緒
 * 定期連到 127.0.0.1:<mt_telnet_port>（MT 上的 telnetsrv，見 telnetsrv_bhload.c）,
 * 送 "bhload get" 取得 MT 累積的 DL/UL RB 使用量，換算成忙碌比例後寫入
 * mac->backhaul_prb_ratio（1.0=無約束, 0.0=完全占滿），供 nr_dlsch_preprocessor()
 * 縮小這次排程週期的可用 PRB 池（gNB_scheduler_dlsch.c）。
 *
 * 連線失敗一律 fail-safe 回 1.0（無約束），不維持上次的值也不誤判成 0.0——
 * 這個測試平台有記錄在案的 DU/MT 重啟骨牌效應，暫時連不上不代表 backhaul 真的塞滿。
 */

#include "nr_mac_gNB_backhaul_poll.h"

#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>

#include "common/utils/LOG/log.h"
#include "common/utils/assertions.h"
#include "common/utils/system.h"

// 這個測試平台目前所有節點都固定用 numerology 1（30kHz SCS，slot=0.5ms）、106 PRB
// 載波，故直接寫死做正規化分母；若之後改變 numerology/頻寬設定，這兩個常數要同步調整。
#define BH_POLL_CARRIER_PRB 106
#define BH_POLL_SLOTS_PER_SEC 2000
// [Race condition 修復 2026-09-12 第一版] 原本 300ms 一次「開新連線→查詢→關閉」
// 會導致 MT 端 telnetsrv 在極少數情況下 buffer overflow 崩潰（*** buffer overflow
// detected ***）——根因是 telnetsrv 的 run_telnetsrv() 對每一次新連線都會呼叫 GNU
// readline 的 read_history()/add_history()/write_history()/stifle_history()
// （common/utils/telnetsrv/telnetsrv.c），這是設計給人類/低頻互動 shell 用的機制。
// 第一版修復把間隔拉長到 3000ms（10倍），原以為足夠把觸發機率壓到接近這個專案既有
// channelmod 用法（每 60 秒一次）的安全頻率，但在後續 15 分鐘級的實際量測中，PC1
// 上的 MT-2/7/8 仍然各自復現了這個崩潰（單一節點就有 9 次），證實「拉長間隔」只是
// 降低機率、不是消除觸發條件——只要還是「每次輪詢都開一條新連線」，就一定會不斷
// 重新觸發 read_history()/write_history()，機率只會隨輪詢次數累積趨近於 1。
// [Race condition 修復 2026-09-12 第二版，根本修法] 改成整個 poll thread 生命週期
// 只在第一次成功時建立「一條長連線」，之後每次輪詢都在同一條連線上重複送
// "bhload get"，不再每輪重新 connect/close——這樣 read_history() 等函式整個
// process 生命週期只會在「連線建立那一刻」被呼叫一次（分析同一個 race 的觸發條件：
// 每一次新連線 = 一次觸發機會，這版把觸發機會從「每 3 秒一次」降到「幾乎只在
// process 啟動或連線異常斷線後重連時發生一次」，數量級對齊、甚至優於既有
// channelmod 用法的安全頻率）。連線若中途斷掉（MT 重啟、網路暫時不通等），才會
// 重新建立一條新連線——這是設計上無法避免的唯一殘留觸發點，但發生頻率跟這個測試
// 平台本身的 MT 重啟頻率掛鉤，而不是跟輪詢週期掛鉤，遠低於原本的問題規模。
#define BH_POLL_INTERVAL_MS 3000
#define BH_POLL_EWMA_ALPHA 0.3f
#define BH_POLL_RECV_TIMEOUT_SEC 1

typedef struct {
  gNB_MAC_INST *mac;
  int mt_telnet_port;
  char mt_telnet_addr[64];
} backhaul_poll_args_t;

// 建立一條新連線並丟掉 telnetsrv 的歡迎 banner，之後這條連線會被重複使用到失敗為止。
static int bh_open_connection(const char *ip, int port)
{
  int sock = socket(AF_INET, SOCK_STREAM, 0);
  if (sock < 0)
    return -1;

  struct timeval tv = {.tv_sec = BH_POLL_RECV_TIMEOUT_SEC, .tv_usec = 0};
  setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
  setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));

  struct sockaddr_in addr;
  memset(&addr, 0, sizeof(addr));
  addr.sin_family = AF_INET;
  addr.sin_port = htons((uint16_t)port);
  addr.sin_addr.s_addr = inet_addr(ip);

  if (connect(sock, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
    close(sock);
    return -1;
  }

  // telnetsrv 連線後會先送一段歡迎/banner 訊息，短暫等待讓它先流過緩衝區，
  // 避免被誤判成命令的回應（沿用 channelmod_ctrl.py 既有的 connect-drain-send-recv 模式）。
  // 這個 drain 只在「建立連線那一刻」做一次，之後同一條連線重複查詢不會再收到 banner。
  usleep(100000);
  char drain[512];
  int flags_save = fcntl(sock, F_GETFL, 0);
  fcntl(sock, F_SETFL, flags_save | O_NONBLOCK);
  while (recv(sock, drain, sizeof(drain), 0) > 0) { /* drain banner */
  }
  fcntl(sock, F_SETFL, flags_save);

  return sock;
}

// 在既有連線上送一次 "bhload get" 查詢。回傳 -1 代表這條連線已經壞掉（送失敗、
// 對面關閉、逾時都算），呼叫端要把 sock 關掉、下一輪重新 bh_open_connection()。
//
// [2026-09-12 長連線 bug 修復] 第一版長連線改法只在 bh_open_connection() 做一次
// banner drain，之後每輪直接 send+單次 recv，忽略了 telnetsrv 是互動式 shell：
// 每次處理完一個命令，除了我們要解析的 "dl_rb_cum ... ul_rb_cum ..." 回應之外，
// 通常還會再印一段 shell prompt（例如 "softmodem_5Gue>"）。在同一條長連線上，
// 這段 prompt 常常跟*下一次*查詢的回應一起被 TCP 合併／延遲送達，導致某一輪的
// recv() 意外先讀到「上一輪」殘留在核心緩衝區裡的 prompt 字串、sscanf 解析失敗、
// 回傳 -1，呼叫端因此把這條連線關掉重開——等於長連線的效果被自己的 parsing bug
// 完全抵銷，退化回「幾乎每輪都重新連線」，繼續高頻觸發 telnetsrv 那個 readline
// history race（PC3 Node9~12 在 2026-09-12 深夜這裡復發到單一 MT 40+ 次崩潰，
// 就是這個退化造成的）。修法：送出新指令前，先用非阻塞 recv 把核心緩衝區裡任何
// 殘留資料（上一輪的 prompt）清乾淨，再送指令、收回應。
static int bh_query(int sock, unsigned long *dl_rb_cum, unsigned long *ul_rb_cum)
{
  char drain[512];
  int flags_save = fcntl(sock, F_GETFL, 0);
  fcntl(sock, F_SETFL, flags_save | O_NONBLOCK);
  while (recv(sock, drain, sizeof(drain), 0) > 0) { /* 清掉上一輪殘留的 prompt */
  }
  fcntl(sock, F_SETFL, flags_save);

  // [2026-09-12 修復] "get" 改名成 "query"，見 telnetsrv_bhload.c 的說明（撞上
  // telnetsrv.c 保留給模組變數存取語法的 "get"/"set" 關鍵字，會導致 MT 端 SIGSEGV）。
  const char *cmd = "bhload query\n";
  if (send(sock, cmd, strlen(cmd), MSG_NOSIGNAL) < 0)
    return -1;

  char buf[256];
  ssize_t n = recv(sock, buf, sizeof(buf) - 1, 0);
  if (n <= 0)
    return -1;
  buf[n] = '\0';

  if (sscanf(buf, "%*[^0-9]%lu%*[^0-9]%lu", dl_rb_cum, ul_rb_cum) != 2)
    return -1;

  return 0;
}

static void *bh_poll_thread(void *arg)
{
  backhaul_poll_args_t *args = (backhaul_poll_args_t *)arg;
  gNB_MAC_INST *mac = args->mac;
  int port = args->mt_telnet_port;
  const char *ip = args->mt_telnet_addr;

  unsigned long prev_dl = 0, prev_ul = 0;
  bool have_prev = false;
  float smoothed_busy = 0.0f;

  LOG_I(NR_MAC, "[Backhaul-aware PRB budget] 輪詢執行緒啟動，連線目標 %s:%d\n", ip, port);

  const long max_rb_per_interval = 2L * BH_POLL_CARRIER_PRB * BH_POLL_SLOTS_PER_SEC * BH_POLL_INTERVAL_MS / 1000;

  int sock = -1; // 長連線：只在第一次或斷線後重新 open，其餘每輪重複用同一條

  while (1) {
    usleep(BH_POLL_INTERVAL_MS * 1000);

    if (sock < 0) {
      sock = bh_open_connection(ip, port);
      if (sock < 0) {
        // 連不上（MT 還沒起來、暫時重啟中...）一律 fail-safe 回無約束，不要誤判成塞滿
        atomic_store_explicit(&mac->backhaul_prb_ratio, 1.0f, memory_order_relaxed);
        have_prev = false;
        continue;
      }
    }

    unsigned long dl_rb_cum = 0, ul_rb_cum = 0;
    if (bh_query(sock, &dl_rb_cum, &ul_rb_cum) != 0) {
      // 這條長連線壞了（MT 重啟、暫時斷線...）：關掉，下一輪重新建立連線。
      // 一樣 fail-safe 回無約束，不要誤判成塞滿。
      close(sock);
      sock = -1;
      atomic_store_explicit(&mac->backhaul_prb_ratio, 1.0f, memory_order_relaxed);
      have_prev = false;
      continue;
    }

    if (!have_prev) {
      // 第一次成功輪詢，還沒有 delta 基準，先記錄起點，這一輪維持無約束
      prev_dl = dl_rb_cum;
      prev_ul = ul_rb_cum;
      have_prev = true;
      atomic_store_explicit(&mac->backhaul_prb_ratio, 1.0f, memory_order_relaxed);
      continue;
    }

    long delta = (long)((dl_rb_cum + ul_rb_cum) - (prev_dl + prev_ul));
    prev_dl = dl_rb_cum;
    prev_ul = ul_rb_cum;
    if (delta < 0)
      delta = 0; // MT 端重啟過，累積值歸零，這一輪先當作 0 忙碌度，下一輪會恢復正常

    float instant_busy = (float)delta / (float)max_rb_per_interval;
    if (instant_busy > 1.0f)
      instant_busy = 1.0f;

    // Scenario R 的流量刻意做成 bursty，瞬時值會太跳動，做輕度 EWMA 平滑
    smoothed_busy = BH_POLL_EWMA_ALPHA * instant_busy + (1.0f - BH_POLL_EWMA_ALPHA) * smoothed_busy;

    float ratio = 1.0f - smoothed_busy;
    if (ratio < 0.0f)
      ratio = 0.0f;
    atomic_store_explicit(&mac->backhaul_prb_ratio, ratio, memory_order_relaxed);
  }

  return NULL;
}

void start_backhaul_poll_thread(gNB_MAC_INST *mac, int mt_telnet_port, const char *mt_telnet_addr)
{
  if (mt_telnet_port <= 0) {
    // 沒有設定 MT telnet port（例如 Donor 沒有 MT）：不啟動輪詢，維持初始值 1.0（無約束）
    return;
  }

  backhaul_poll_args_t *args = calloc(1, sizeof(backhaul_poll_args_t));
  AssertFatal(args != NULL, "couldn't allocate backhaul_poll_args_t\n");
  args->mac = mac;
  args->mt_telnet_port = mt_telnet_port;
  snprintf(args->mt_telnet_addr, sizeof(args->mt_telnet_addr), "%s", mt_telnet_addr ? mt_telnet_addr : "127.0.0.1");

  pthread_t t;
  threadCreate(&t, bh_poll_thread, args, "backhaul_poll", -1, OAI_PRIORITY_RT_LOW);
}
