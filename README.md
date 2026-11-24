# SOYAL Access Control

將 SOYAL 門禁控制器整合至 Home Assistant。目前正式版為 **v0.8.0**。

## 功能介紹

- 本地 TCP／RS485 門禁控制
- 200 ms 解鎖輸出監控
- 單次開門、常開及解除常開
- 常開超時自動解除
- FIFO 自動同步門禁事件
- SQLite 本地歷史紀錄
- HA 操作及使用者紀錄
- 自訂站號 1–254
- TCP 自動重連

## 支援設備與連線架構

實機驗證設備為 **SOYAL AR-725E** 與 **EP-E300-485 Gateway**，已確認站號 01 與 02。

`Home Assistant → TCP Gateway → RS485 → SOYAL`

每個 Gateway 通訊端點僅監控一台控制器。多台控制器請使用獨立 Gateway 或獨立通訊通道。

## HACS 安裝方式

1. 在 HACS 的「自訂儲存庫」加入本專案，類型選 **Integration**。儲存庫網址為 `https://github.com/JK0526/ha-soyal-access`。
2. 下載 **SOYAL Access Control v0.8.0**，然後重新啟動 Home Assistant。
3. 到「設定 → 裝置與服務 → 新增整合」，搜尋 **SOYAL Access Control**。

## 整合設定方式

| 欄位 | 設定 |
| --- | --- |
| Gateway Host | Gateway 的 IP 位址或主機名稱 |
| TCP Port | Gateway 的 TCP 通訊埠，預設 502 |
| Node ID | 控制器實際站號，預設 2，整數範圍 1–254 |

變更設定請使用整合的「重新設定」。切換站號前，請先確認現場並解除原站號的常開狀態。

## Dashboard 與歷史查詢教學

原生 Dashboard、Helper、SQL 查詢自動化與安裝步驟見 [範本教學](examples/README.md)。版本資訊見 [CHANGELOG.md](CHANGELOG.md)。

## 注意事項

- HA 停機、整合未載入或通訊異常時，無法保證常開超時功能準時解除。
- 解鎖輸出反映控制器訊號，無法據此判定實際門扇或鎖舌位置。
- 請避免其他程式同時主動操作相同 RS485 通訊通道。

## License

本儲存庫目前未附授權檔。
