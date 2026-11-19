# SOYAL Access Control

`v0.7.0-beta.3` 是 Stage 3 預發行版。Home Assistant domain 為 `soyal_access`。本版提供 Gateway TCP 狀態、Node 02 手動唯讀 I/O 查詢與脈衝開門。既有 `uncertain` 安全封鎖仍有效。

## HACS 安裝或更新

1. 若 HACS 尚未有此套件，在自訂儲存庫新增 `https://github.com/JK0526/ha-soyal-access`，類型選 Integration。
2. 開啟 SOYAL Access Control 的下載或重新下載選單，指定 `v0.7.0-beta.3`，完成後重新啟動 Home Assistant。
3. 已有 SOYAL Config Entry 會沿用原設定。首次安裝從「設定 → 裝置與服務 → 新增整合」選 SOYAL Access Control，輸入 Host 與 TCP Port。

升級保留舊 Entity Registry、SQLite 及 `.storage` 封鎖紀錄。Node 02 脈衝開門沿用原有實體識別，預設停用。上一版程式與操作文件保存在 [v0.6.0-beta.1](https://github.com/JK0526/ha-soyal-access/tree/v0.6.0-beta.1)。版本來源見 [manifest.json](custom_components/soyal_access/manifest.json)，歷史版本見 [CHANGELOG](CHANGELOG.md)。

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

## Node 02 實機驗收

1. 更新並重啟 HA，確認 Gateway TCP 顯示已連線。Node 02「手動查詢」在每次載入後預設停用。
2. 現場管理員先確認舊交易已結束、Gateway 和 RS485 路徑已排空舊回應，並隔離共用匯流排上的其他發送端。請依廠商程序或獨立診斷取得排空證據。只有等待或重新連線時，維持停用。唯讀查詢流程不會讀寫舊版 `uncertain` 紀錄。
3. 在「開發者工具 → 動作」以登入的管理員執行 `soyal_access.confirm_node02_query`，選擇對應的 SOYAL Config Entry，勾選「已查核排空證據」。確認動作只允許當次載入期間的查詢，本身不送封包。
4. 在 Node 02 按下「手動查詢」。確認「最近查詢結果」顯示查詢成功，記錄 `last_success` 與 `elapsed_ms`。可在完成後再次手動查詢；前一筆在途時會拒絕重複操作。
5. 若逾時、收到不完整或不明回應、或交易期間斷線，整合關閉 Session 並停止發送。再次測試前，先確認原因與取得新的現場排空證據，再重載並由管理員確認。TCP 重連不會自動啟用查詢。

唯讀查詢的發送指令為 Node 02 `0x21 / 00 00`。預期單一完整的 Node 02 I/O 資料回應；同一批收到額外資料會視為不明回應。耗時自送出前至收到完整有效回應，包含 TCP 與 RS485 往返。

## Node 02 脈衝開門現場驗收

1. 先完成上述 TCP 與手動唯讀查詢驗收。確認既有交易已結束、Gateway 與 RS485 路徑已排空，且現場門禁可安全測試。
2. 以登入的 HA 管理員在「開發者工具 → 動作」執行 `soyal_access.confirm_node02_pulse`，選擇整合並勾選「已確認排空與門禁安全」。唯讀授權不會啟用開門。整合只在開門啟用時唯讀檢查原端點的舊封鎖檔；若 Node 02 仍列為未決或檔案無法驗證，維持停用。管理員確認是現場查核聲明，整合無法自行觀測緩衝區。
3. 按一次「脈衝開門」。整合送一次 Node 02 `0x21 / 84`。匹配的 11 位元組 I/O 回應或 7 位元組 ACK 代表通訊成功，請另以人工確認電鎖實際動作。匹配的 NACK 會回報拒絕並停用本次載入的開門。
4. 若結果未決，停止測試。已送出後遇到逾時、斷線、取消或格式錯誤會關閉 TCP 並停用發送。重載或重啟後授權需重新確認，現場仍須提供新的排空證據。控制不會自動重送。

兩個按鈕共用一筆在途交易。交易期間仍顯示原有可用狀態，重複操作會在發送前被拒絕。TCP 或有效回應均不代表實體門鎖位置。
