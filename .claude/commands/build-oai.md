編譯 OAI RAN with E2 Agent（gNB + nrUE）。

步驟：
```bash
cd ~/openairinterface5g/cmake_targets
sudo ./build_oai --gNB --nrUE --build-e2 --ninja -w USRP -C \
  --cmake-opt -DE2AP_VERSION=E2AP_V2 \
  --cmake-opt -DKPM_VERSION=KPM_V3_00 \
  --cmake-opt -DCMAKE_BUILD_TYPE=Release
```

注意：
- `-C` 會清除舊的 build cache，首次或有結構性修改時使用
- 若只改 C 語言邏輯（非 CMakeLists），可移除 `-C` 加速編譯
- 完成後確認 `cmake_targets/ran_build/build/nr-softmodem` 存在

編譯完成後回報結果與任何 warning/error。
