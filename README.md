# SOYAL Access Control

`v0.7.1-beta.1` 是 Stage 8A 預發行版。Home Assistant domain 為 `soyal_access`。本版提供 Gateway TCP 狀態、Node 02 手動唯讀 I/O 查詢、脈衝開門與常開控制。本版將手動讀取的 Node 02 FIFO 最舊事件保存到本機 SQLite。

## HACS 安裝或更新

1. 若 HACS 尚未有此套件，在自訂儲存庫新增 `https://github.com/JK0526/ha-soyal-access`，類型選 Integration。
2. 開啟 SOYAL Access Control 的下載或重新下載選單，指定 `v0.7.1-beta.1`，完成後重新啟動 Home Assistant。
3. 已有 SOYAL Config Entry 會沿用原設定。首次安裝從「設定 → 裝置與服務 → 新增整合」選 SOYAL Access Control，輸入 Host 與 TCP Port。

升級保留舊 Entity Registry、SQLite 及 `.storage` 封鎖紀錄。Node 02 脈衝開門沿用 beta.3 的 Entity ID 與 Unique ID。上一版程式與操作文件保存在 [v0.6.0-beta.1](https://github.com/JK0526/ha-soyal-access/tree/v0.6.0-beta.1)。版本來源見 [manifest.json](custom_components/soyal_access/manifest.json)，歷史版本見 [CHANGELOG](CHANGELOG.md)。

## EP-E300-485 現場驗收

1. 確認 EP-E300-485（韌體 V2.2.9）設為 RAW TCP Server。核對既有整合的 Host 為 `192.168.1.208`、Port 為 `502`。需要更正時使用「重新設定」。
2. 載入或重載整合，在 **SOYAL Gateway** 查看 **TCP 連線狀態**。建連成功顯示已連線，建連失敗顯示未連線，HA 日誌會記錄建連錯誤。
3. 在受控時段從 Gateway 端關閉這條 TCP Session，確認 HA 更新為未連線。讓 Gateway 恢復接受連線後，觀察自動重連及 Node 02 在線狀態恢復。
4. 卸載或停止 HA 後，從 Gateway 端確認該 Session 關閉。Gateway TCP 與 Node 02 在線請分開核對。

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

1. 確認 Gateway TCP 顯示已連線。連線正常且沒有進行中的交易時，「手動查詢」、「脈衝開門」與「常開」可直接操作。
2. 每次只有一筆交易可在途。重複操作會在發送前被拒絕。查詢、脈衝開門、常開及解除常開共用同一 TCP Session。
3. 現場操作前，由管理員確認 RS485 匯流排及門禁環境適合測試。舊版 uncertain 紀錄保持不變，新版不以該紀錄作為按鈕授權條件。保留紀錄不代表舊回應已排空，也不代表排空已驗證。
4. 脈衝開門送一次 Node 02 `0x21 / 84`，收到匹配 I/O 回應或 ACK 表示通訊交易成功。NACK 表示控制器拒絕。請另行人工確認門鎖實際動作。
5. 若交易已送出但遇到逾時、斷線、取消或無效回應，結果顯示未確認並關閉目前 Session。不會自動重送。HA Reload 可建立新連線，但重連不證明 Gateway 舊回應已排空。查明現場狀況後再操作。

SOYAL 回應沒有交易識別碼。新版會拒絕閒置時收到的非預期資料，但如果舊回應在新交易送出後才抵達，而且內容與預期回應相同，整合無法辨別兩者。遇到未確認的交易，請先在現場查明舊回應的去向，再繼續測試。


## Node 02 常開驗收

在 HACS 更新並重啟後，先確認 Gateway TCP、手動查詢與脈衝開門，再於受控現場對 Node 02 執行一次「常開」，確認持續解鎖。接著執行一次「解除常開」，確認門禁恢復正常控制，並核對沒有重複動作。

「常開」開關採用 assumed state，啟動或 Reload 後從 Unknown 開始。即使顯示 Unknown，仍可明確執行開啟或關閉，或透過 HA 的 `switch.turn_on`／`switch.turn_off` 操作該實體。收到合法的 I/O 或 ACK 回應後，開關才會顯示最後成功下達的命令。NACK 保留上一個成功命令。交易結果不明時，清除命令狀態並關閉 Session，開關因此暫時顯示不可用。這個開關沒有實體鎖舌、門磁或 Relay 回授，請在現場確認電鎖動作。

## Stage 5 現場驗收

請透過 HACS 指定安裝 `v0.7.1-beta.1` 預發行版。

- Node 02 每 200 ms 排定一次 `0x21 / 00 00` 唯讀輪詢。單調時鐘與單一背景工作控制週期，忙碌或超過週期時不補送。控制與手動查詢共用 Transport。最多一筆前景操作可等待已送出的輪詢結束，等待期間略過新輪詢。重複前景操作直接拒絕。
- Gateway TCP 與 Node 02 Online 各自呈現。有效輪詢 I/O 回應才可點亮 Node 02 Online。連續三筆可確認的失敗（例如 NACK）會熄滅，TCP 斷線則立即熄滅，下次有效輪詢可重新點亮。背景輪詢不會更新「最近查詢結果」或「常開」狀態。
- 斷線後退避 1、2、4、8、15、30 秒，最高維持 30 秒。TCP 建連本身不重設退避，完成下述兩筆有效輪詢驗證才重設。重連只建立 TCP 並恢復唯讀輪詢，從不重播控制。
- 非預期資料、格式不符或已送出的交易逾時會關閉該 Socket。每條新連線（含 HA Reload）先靜默觀察一個設定的交易逾時，至少 200 ms。這讓新連線有一個與既有等待回應窗口相同的觀察期。靜默期間不送指令，收到資料就重新隔離連線。之後必須取得兩筆連續有效的唯讀 I/O 輪詢回應，才恢復手動查詢和門鎖控制。第一筆有效回應即可點亮 Node 02 Online，控制仍須等第二筆。驗證失敗時保留原有退避，成功後自動恢復，沒有永久封鎖或控制重播。有限的觀察窗口無法證明 Gateway 的所有舊回應都已排空。
- SOYAL 無交易識別碼。若其他 TCP Client 的回應在 HA 交易期間抵達，且內容完全符合預期，SOYAL 封包本身無法識別發送者。現場須確保同一 RS485 匯流排只有 HA 主動發送。若外部 Monitor 發送後觸發 HA 斷線，可能是 Gateway 將該回應也送到 HA，讓 HA 判為閒置資料或不符目前交易的回應。實際轉發方式須由現場觀察確認。

現場先隔離外部發送端，再觀察約 200 ms 的正常輪詢及 Gateway／Node 指示。逐筆驗證脈衝、常開、解除常開及手動查詢，記錄控制等待已發送輪詢的時間。用受控斷線觀察退避、Node 離線及唯讀恢復，確認連線恢復後沒有自行開門。若回應歸屬不明，記錄前後兩個狀態、操作名稱和時間，確認沒有自動重送，並觀察完成兩筆有效輪詢後控制是否恢復可用。

## Stage 6 主門解鎖輸出驗收

1. 在 HACS 重新下載並選擇 `v0.7.1-beta.1`，重啟 HA 後確認 Node 02 裝置下有「主門解鎖輸出」。
2. 待機時確認 OFF。受控測試脈衝開門、常開、解除常開時，依序確認短暫 ON、維持 ON、回到 OFF。
3. 受控斷線時確認 Unavailable，恢復後確認感測器採用最新輪詢資料。

感測器顯示控制器的解鎖輸出。實體鎖舌位置須現場確認。

## Stage 7 FIFO 唯讀驗收

在測試分支的 Node 02 裝置按一次「讀取 FIFO 最舊事件」。檢查「FIFO 讀取結果」與控制器時間、事件碼、站號、Port 及 User Address。時間採控制器本地 RTC，無效時間顯示為空值。未辨識的事件碼保留數值，未知資料仍留在原始封包中。原始資料可能含敏感資訊，請勿公開轉貼。

本階段接受 Node 02 的 7-byte ACK、NACK 與已知 31-byte 事件框架。ACK 分類為空佇列，仍須以現場資料核對控制器的實際空佇列格式。NACK 代表拒絕。`0x25` 只讀取 FIFO 最舊事件，不會移除紀錄。重複查詢可能讀到同一筆事件，此版本不提供即時刷卡事件串流。此操作不推進 FIFO，因此無法看到還排在後方的記錄。

## Stage 8A FIFO 觀察紀錄

手動 `0x25` 收到有效事件後，整合釋放 TCP 交易鎖，再透過 HA Executor 將事件提交到 `<HA_CONFIG>/soyal_access/<entry_id>.sqlite3`。每個 Config Entry 各用一個檔案。首次有合法事件時才初始化資料庫，並用 `fifo_observation_schema` 管理獨立版本。Reload 或 Restart 後可重新開啟同一份資料庫。

`fifo_observations` 保存原始封包、來源、事件欄位、首次與最後觀察時間及讀取次數。控制器 RTC 仍以本地時間保留。相同 Entry、Node 與原始封包 SHA-256 視為同一筆 FIFO 頭部的重複觀察。這個規則不能用來推斷不同 FIFO 位置的事件是同一筆通行紀錄。

「FIFO 讀取結果」會顯示 `storage_status`。首次提交為 `saved`，重複觀察為 `already_saved`，初始化、寫入或提交失敗為 `error`。儲存失敗仍保留有效的 FIFO 讀取結果，且輪詢與門鎖控制照常執行。PIN 事件的原始封包只留在本機資料庫，不會顯示在感測器欄位或日誌。此階段只觀察 FIFO 最舊事件，沒有移除命令。


### 資料庫備份與還原

備份前先卸載該 Config Entry（保留設定）或停止 Home Assistant，等正在寫入的觀察紀錄提交並關閉 SQLite 連線，再複製資料庫。若使用 HA 備份，請確認備份確實包含 `soyal_access/` 目錄。還原時也應在整合卸載或 HA 停止期間進行，並放回同一 Config Entry 的檔名。備份含原始門禁事件，請限制存取。

Stage 8A 只在按下「讀取 FIFO 最舊事件」後保存該次觀察，不會自動同步，也不會移除控制器上的 FIFO 事件。

## Stage 8B 自動同步與移除驗收

「FIFO 自動同步」保存使用者期望狀態（`desired_auto_sync`）。舊版設定不會直接授權自動消費。首次啟用前先備份 SQLite、確認 HA 是唯一的 FIFO 消費端，並由管理員在 OFF 時執行下述確認服務，建立安全基準。重新載入後只有期望為 ON、SQLite 安全狀態為 `safe`、沒有未決移除或協定暫停，且通訊通過驗證，才會恢復。每輪會先提交 `in_flight`，安全結束才提交 `safe`。同步狀態的 `desired_auto_sync` 與 `pause_reason` 可分別查看期望與封鎖原因。

首次實機驗收請保持自動同步 OFF，先以「讀取最舊 FIFO 事件」核對頭部，再按 Node 02 的「同步並移除單筆 FIFO 事件」一次。透過「FIFO 同步狀態」的 `last_single_sync_result` 查看結果。`popped_confirmed` 表示已確認移除並提交 SQLite，可用 `last_event_id` 核對本機正式紀錄，再用手動唯讀查詢核對下一筆。`pop_uncertain` 表示移除結果未明，應停止驗收並檢查持久化進度。按鈕不會自動再讀下一筆，也不會切換自動同步。

若同步讀取收到無法配對的回應，例如 `0x25` 期間收到可與移除 ACK 混淆的 12-byte 封包，整合會隔離該 TCP Session 並將 `fifo_read_protocol_error` 寫入 SQLite。TCP 重新驗證後，I/O 輪詢與門禁控制可獨立恢復。FIFO 同步維持 `paused`，切換同步開關或重新載入都不會解除。人工唯讀查詢仍可用於診斷，單筆消費同樣受暫停限制。

確認干擾來源已排除，並核對現場回應及 SQLite 未決紀錄後，保持自動同步 OFF。由管理員呼叫 `soyal_access.clear_fifo_pause`，傳入該 SOYAL Config Entry 的 `entry_id` 與 `verified: true`。這只清除協定暫停，不送任何指令、不切換自動同步，也不清除未決移除。未決移除仍會阻擋自動同步與單筆消費，請保留資料庫並用手動唯讀查詢調查。若仍收到無法配對的回應，重新啟用後會再安全暫停。

Stage 8B 在每個 Entry 的既有 SQLite 加入版本為 1 的 `fifo_sync_schema`、`fifo_sync_progress` 與 `fifo_access_events`。舊 `fifo_observations` 不做回填或覆寫。正式紀錄使用本地序號。前一筆已確認移除後，即使下一筆的原始資料完全相同，也會另存一筆。

每輪先讀取頭部並提交正式事件，再提交 `pop_uncertain` 刪除意圖。在同一條 TCP Session 重新讀取並核對原始頭部後，才允許送一次 `0x37`。只有收到已實測格式的 12-byte Node 02 ACK 且 SQLite 完成提交，才記為 `popped_confirmed`。NACK、逾時、斷線、意圖提交後的重啟，或 ACK 後提交失敗，都可能留下待確認狀態。單靠重連、相同頭部或空佇列候選 ACK 不會重送或解開待確認。未決移除需另行調查，可關閉自動同步並使用手動唯讀查看。切換開關不清除未決紀錄。

SQLite 工作在 Executor 執行，不持有 TCP 交易鎖。控制與手動操作優先於尚未送出的自動交易。同時只處理一個頭部，且每次成功後至少讓出 200 ms。空佇列、暫停或待確認時約每秒唯讀檢查。關閉同步不會中斷已送出的移除，仍會處理回應及提交進度。

有限的封包驗證無法辨別所有遲到回應，SOYAL 也沒有原子「比對並移除」指令。現場須維持單一 FIFO 消費者，避免其他程式同時移除或清空。還原較舊的資料庫備份後，應在 HA 啟動前停用自動同步或隔離 Gateway，再人工核對佇列位置與進度，以免舊備份遺失後續的未決意圖。

變更 Gateway 主機或連接埠時會停用自動同步。正式 FIFO 進度綁定原端點，避免把舊控制器的未決紀錄套用到另一台控制器。若要使用不同的 Gateway，請建立獨立的 Config Entry。
