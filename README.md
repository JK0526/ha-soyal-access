# SOYAL Access Control

`v0.7.0-beta.4` 是 Stage 3 預發行版。Home Assistant domain 為 `soyal_access`。本版提供 Gateway TCP 狀態、Node 02 手動唯讀 I/O 查詢與脈衝開門。TCP 正常時可直接操作。

## HACS 安裝或更新

1. 若 HACS 尚未有此套件，在自訂儲存庫新增 `https://github.com/JK0526/ha-soyal-access`，類型選 Integration。
2. 開啟 SOYAL Access Control 的下載或重新下載選單，指定 `v0.7.0-beta.4`，完成後重新啟動 Home Assistant。
3. 已有 SOYAL Config Entry 會沿用原設定。首次安裝從「設定 → 裝置與服務 → 新增整合」選 SOYAL Access Control，輸入 Host 與 TCP Port。

升級保留舊 Entity Registry、SQLite 及 `.storage` 封鎖紀錄。Node 02 脈衝開門沿用 beta.3 的 Entity ID 與 Unique ID。上一版程式與操作文件保存在 [v0.6.0-beta.1](https://github.com/JK0526/ha-soyal-access/tree/v0.6.0-beta.1)。版本來源見 [manifest.json](custom_components/soyal_access/manifest.json)，歷史版本見 [CHANGELOG](CHANGELOG.md)。

## EP-E300-485 現場驗收

1. 確認 EP-E300-485（韌體 V2.2.9）設為 RAW TCP Server。核對既有整合的 Host 為 `192.168.1.208`、Port 為 `502`。需要更正時使用「重新設定」。
2. 載入或重載整合，在 **SOYAL Gateway** 查看 **TCP 連線狀態**。建連成功顯示已連線，建連失敗顯示未連線，HA 日誌會記錄建連錯誤。
3. 在受控時段從 Gateway 端關閉這條 TCP Session，確認 HA 更新為未連線。讓 Gateway 恢復接受連線後，手動重載整合，確認回到已連線。此階段由載入觸發連線，沒有自動重連排程。
4. 卸載或停止 HA 後，從 Gateway 端確認該 Session 關閉。驗收範圍是 TCP Socket；AR-725E 在線另待後續階段驗證。

## 檔案分工

| 檔案 | 職責 |
| --- | --- |
| `__init__.py` | HA 載入、重載、卸載與停止 |
| `config_flow.py` | Host、Port 設定與重複端點檢查 |
| `hub.py` | 每筆 Config Entry 持有一個 Transport |
| `transport.py` | 建立 TCP，接收資料以偵測 EOF，關閉背景工作及 Socket |
| `binary_sensor.py` | 使用原有識別發布 Gateway TCP 狀態 |
| `const.py`、`manifest.json`、翻譯與品牌資源 | 整合註冊與 HACS 包裝 |
| `protocol.py` | SOYAL 原有編解碼 |

## Node 02 手動操作

1. 確認 Gateway TCP 顯示已連線。連線正常且沒有進行中的交易時，「手動查詢」與「脈衝開門」可直接操作。
2. 每次只有一筆交易可在途。重複操作會在發送前被拒絕。查詢與開門共用同一 TCP Session。
3. 現場操作前，由管理員確認 RS485 匯流排及門禁環境適合測試。舊版 uncertain 紀錄保持不變，新版不以該紀錄作為按鈕授權條件。保留紀錄不代表舊回應已排空，也不代表排空已驗證。
4. 脈衝開門送一次 Node 02 `0x21 / 84`，收到匹配 I/O 回應或 ACK 表示通訊交易成功。NACK 表示控制器拒絕。請另行人工確認門鎖實際動作。
5. 若交易已送出但遇到逾時、斷線、取消或無效回應，結果顯示未確認並關閉目前 Session。不會自動重送。HA Reload 可建立新連線，但重連不證明 Gateway 舊回應已排空。查明現場狀況後再操作。

SOYAL 回應沒有交易識別碼。新版會拒絕閒置時收到的非預期資料，但如果舊回應在新交易送出後才抵達，而且內容與預期回應相同，整合無法辨別兩者。遇到未確認的交易，請先在現場查明舊回應的去向，再繼續測試。
