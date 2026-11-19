# SOYAL Access Control

`v0.7.0-beta.1` 是重新開發的 Stage 0 + Stage 1 TCP-only 預發行版。Home Assistant domain 為 `soyal_access`。本版只提供 **SOYAL Gateway → TCP 連線狀態**。門鎖控制尚未提供，既有 `uncertain` 安全封鎖並未解除。

## HACS 安裝或更新

1. 若 HACS 尚未有此套件，在自訂儲存庫新增 `https://github.com/JK0526/ha-soyal-access`，類型選 Integration。
2. 開啟 SOYAL Access Control 的下載或重新下載選單，指定 `v0.7.0-beta.1`，完成後重新啟動 Home Assistant。
3. 已有 SOYAL Config Entry 會沿用原設定。首次安裝從「設定 → 裝置與服務 → 新增整合」選 SOYAL Access Control，輸入 Host 與 TCP Port。

升級保留舊 Entity Registry、SQLite 及 `.storage` 封鎖紀錄。舊門禁實體依 HA 的既有登錄顯示，沒有可執行的控制功能。上一版程式與操作文件保存在 [v0.6.0-beta.1](https://github.com/JK0526/ha-soyal-access/tree/v0.6.0-beta.1)。版本來源見 [manifest.json](custom_components/soyal_access/manifest.json)，歷史版本見 [CHANGELOG](CHANGELOG.md)。

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
| `protocol.py` | 保留離線編解碼基礎，TCP 階段載入路徑未引用 |

此版本建立 Socket 後只接收資料以偵測斷線，沒有 SOYAL 或 RS485 發送路徑。
