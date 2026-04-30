編譯 FlexRIC xApp。

步驟：
1. 進入 build 目錄：`cd ~/openairinterface5g/openair2/E2AP/flexric/build`
2. 執行 cmake：`cmake -G Ninja -DCMAKE_BUILD_TYPE=Release -DKPM_VERSION=KPM_V3_00 -DE2AP_VERSION=E2AP_V2 ..`
3. 執行 ninja 編譯：`ninja`
4. 安裝：`sudo ninja install`

如果有傳入參數（例如 node 編號），只編譯對應的 xApp target。
編譯完成後回報是否成功，列出錯誤行數。
