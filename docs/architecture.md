# SOYAL v0.7.1 架構

每個 Config Entry 保留一個 Hub 與一個 EP-E300-485 RAW TCP Transport。Hub 執行 Node 02 的 200 ms I/O 輪詢、斷線重連及控制排程。Transport 驗證回覆並限制同時只有一筆交易。三種門鎖控制皆由使用者觸發，逾時或斷線後不重送。

HA 狀態分開顯示 Gateway TCP、Node 02 在線及主門解鎖輸出。主門解鎖輸出來自已驗證的 I/O 回應。常開開關呈現命令狀態。

升級時精確移除同一 Config Entry 的舊 FIFO 實體及設定。既有 SQLite 與安全紀錄保留。現場驗收流程見 [README](../README.md)。
