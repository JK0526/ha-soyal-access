# SOYAL Access Control

`v0.8.0-beta.2` 在 `v0.7.2` 基礎上加入背景 FIFO 事件同步；此版本為測試預發行版，現場效能與 Gateway 韌體相容性尚待驗收。整合 domain 為 `soyal_access`。

## HACS 安裝或更新

1. 若 HACS 尚未有此套件，在自訂儲存庫新增 `https://github.com/JK0526/ha-soyal-access`，類型選 Integration。
2. 開啟 SOYAL Access Control 的下載或重新下載選單，指定 `v0.8.0-beta.2`，完成後重新啟動 Home Assistant。
3. 已有 SOYAL Config Entry 會沿用原設定。首次安裝從「設定 → 裝置與服務 → 新增整合」選 SOYAL Access Control，輸入 Host 與 TCP Port。

升級會保留同一 Config Entry、Gateway 設定及現有門禁與狀態實體 ID。FIFO 在背景自動執行，不新增 Home Assistant 實體。新的事件資料寫入 `<HA_CONFIG>/soyal_access_fifo/<entry_id>.sqlite3`；舊版 `<HA_CONFIG>/soyal_access/<entry_id>.sqlite3` 與歷史安全紀錄保持原狀，不會被開啟、覆寫或刪除。版本來源見 [manifest.json](custom_components/soyal_access/manifest.json)，歷史版本見 [CHANGELOG](CHANGELOG.md)。

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
| `transport.py` | 建立 RAW TCP、驗證及配對回覆，關閉接收工作與 Socket |
| `binary_sensor.py` | 發布 Gateway TCP、Node 02 在線及主門解鎖輸出 |
| `const.py`、`manifest.json`、翻譯與品牌資源 | 整合註冊與 HACS 包裝 |
| `protocol.py` | SOYAL 原有編解碼 |
| `fifo.py`、`fifo_storage.py` | 0x25 事件欄位解析與獨立 SQLite 保存 |

## Node 02 手動操作

1. 確認 Gateway TCP 顯示已連線。連線正常且沒有進行中的交易時，「脈衝開門」與「常開」可直接操作。
2. 每次只有一筆交易可在途。重複操作會在發送前被拒絕。背景輪詢、脈衝開門、常開及解除常開共用同一 TCP Session。
3. 現場操作前，由管理員確認 RS485 匯流排及門禁環境適合測試。舊版 uncertain 紀錄保持不變，新版不以該紀錄作為按鈕授權條件。保留紀錄不代表舊回應已排空，也不代表排空已驗證。
4. 脈衝開門送一次 Node 02 `0x21 / 84`，收到匹配 I/O 回應或 ACK 表示通訊交易成功。NACK 表示控制器拒絕。請另行人工確認門鎖實際動作。
5. 若交易已送出但遇到逾時、斷線、取消或無效回應，結果顯示未確認並關閉目前 Session。不會自動重送。HA Reload 可建立新連線，但重連不證明 Gateway 舊回應已排空。查明現場狀況後再操作。

SOYAL 回應沒有交易識別碼。新版會拒絕閒置時收到的非預期資料，但如果舊回應在新交易送出後才抵達，而且內容與預期回應相同，整合無法辨別兩者。遇到未確認的交易，請先在現場查明舊回應的去向，再繼續測試。


## Node 02 常開驗收

在 HACS 更新並重啟後，先確認 Gateway TCP 與脈衝開門，再於受控現場對 Node 02 執行一次「常開」，確認持續解鎖。接著執行一次「解除常開」，確認門禁恢復正常控制，並核對沒有重複動作。

「常開」開關採用 assumed state，啟動或 Reload 後從 Unknown 開始。即使顯示 Unknown，仍可明確執行開啟或關閉，或透過 HA 的 `switch.turn_on`／`switch.turn_off` 操作該實體。收到合法的 I/O 或 ACK 回應後，開關才會顯示最後成功下達的命令。NACK 保留上一個成功命令。交易結果不明時，清除命令狀態並關閉 Session，開關因此暫時顯示不可用。這個開關沒有實體鎖舌、門磁或 Relay 回授，請在現場確認電鎖動作。

## Stage 5 現場驗收

請透過 HACS 指定安裝 `v0.7.2` 正式版。

- Node 02 每 200 ms 排定一次 `0x21 / 00 00` 唯讀輪詢。單調時鐘與單一背景工作控制週期，忙碌或超過週期時不補送。控制與背景輪詢共用 Transport。最多一筆前景操作可等待已送出的輪詢結束，等待期間略過新輪詢。重複前景操作直接拒絕。
- Gateway TCP 與 Node 02 Online 各自呈現。有效輪詢 I/O 回應才可點亮 Node 02 Online。連續三筆可確認的失敗（例如 NACK）會熄滅，TCP 斷線則立即熄滅，下次有效輪詢可重新點亮。背景輪詢不會更新「常開」狀態。
- 斷線後退避 1、2、4、8、15、30 秒，最高維持 30 秒。TCP 建連本身不重設退避，完成下述兩筆有效輪詢驗證才重設。重連只建立 TCP 並恢復唯讀輪詢，從不重播控制。
- 非預期資料、格式不符或已送出的交易逾時會關閉該 Socket。每條新連線（含 HA Reload）先靜默觀察一個設定的交易逾時，至少 200 ms。這讓新連線有一個與既有等待回應窗口相同的觀察期。靜默期間不送指令，收到資料就重新隔離連線。之後必須取得兩筆連續有效的唯讀 I/O 輪詢回應，才恢復門鎖控制。第一筆有效回應即可點亮 Node 02 Online，控制仍須等第二筆。驗證失敗時保留原有退避，成功後自動恢復，沒有永久封鎖或控制重播。有限的觀察窗口無法證明 Gateway 的所有舊回應都已排空。
- SOYAL 無交易識別碼。若其他 TCP Client 的回應在 HA 交易期間抵達，且內容完全符合預期，SOYAL 封包本身無法識別發送者。現場須確保同一 RS485 匯流排只有 HA 主動發送。若外部 Monitor 發送後觸發 HA 斷線，可能是 Gateway 將該回應也送到 HA，讓 HA 判為閒置資料或不符目前交易的回應。實際轉發方式須由現場觀察確認。

現場先隔離外部發送端，再觀察約 200 ms 的正常輪詢及 Gateway／Node 指示。逐筆驗證脈衝、常開、解除常開，記錄控制等待已發送輪詢的時間。用受控斷線觀察退避、Node 離線及唯讀恢復，確認連線恢復後沒有自行開門。若回應歸屬不明，記錄前後兩個狀態、操作名稱和時間，確認沒有自動重送，並觀察完成兩筆有效輪詢後控制是否恢復可用。

## Stage 6 主門解鎖輸出驗收

1. 在 HACS 重新下載並選擇 `v0.7.2`，重啟 HA 後確認 Node 02 裝置下有「主門解鎖輸出」。
2. 待機時確認 OFF。受控測試脈衝開門、常開、解除常開時，依序確認短暫 ON、維持 ON、回到 OFF。
3. 受控斷線時確認 Unavailable，恢復後確認感測器採用最新輪詢資料。

感測器顯示控制器的解鎖輸出。實體鎖舌位置須現場確認。

繁體中文介面中的「主門解鎖輸出」以「解鎖中」與「未解鎖」顯示卡機輸出。這項翻譯保留原有 `on`／`off`、Entity ID 與自動化語意。離線時顯示 HA 原生不可用狀態。升級會移除舊手動查詢按鈕與最近查詢結果感測器。

## v0.8.0-beta.2 FIFO 同步驗收

Hub 沿用單一背景工作、單一 Transport 和一筆在途交易。每個 200 ms 目標時點優先排入 0x21；不補送錯過的輪詢。0x25 每 2 秒最多檢查一次，只在輪詢空檔啟動。有效事件先由 executor 寫入 SQLite 並 Commit，再等一筆有效 0x21 後送 0x37；若輪詢失敗，沿用既有輪詢與重連驗證。空佇列視為正常，不寫資料、不送 0x37。門禁控制先設為忙碌，未開始的 FIFO 操作會讓出通訊時機。

歷史程式與測試記錄的 Node 02 FIFO 事件為 31-byte frame（length 欄位 `0x1D`），事件 payload 為 25 bytes；已知事件碼 `0x0B`、`0x10`、`0x1C`，其餘事件碼保留原始值與封包。空佇列曾記錄 7-byte 短 ACK，也有 12-byte status ACK：`7E 0A 00 04 02 34 10 00 18 73 B6 8B`。0x37 的歷史成功回覆使用同一個 status ACK；NACK 使用 response code `0x05`，並驗證相同格式的 status payload。兩種指令都允許 I/O 位元組隨門鎖與輸入狀態變動，其餘 payload 欄位維持比對。這些向量來自專案歷史，不取代目標 Gateway 韌體的現場封包確認。

請量測無 FIFO、空佇列、有事件及 FIFO timeout 時的 0x21 平均、最大、P95 間隔與 FIFO 交易耗時。測試模擬結果只作離線排程檢查；實機結果須由目標 EP-E300-485／RS485 環境記錄。交易 timeout 會沿用現有 Transport 關閉 TCP 與退避重連，重連後先恢復 0x21；不自動重送 0x37。

本機 Python 3.13 loopback 測試記錄（無 FIFO／空佇列／有事件各 14 個輪詢點，timeout 復原 7 個輪詢點；FIFO 設為 2 秒）：

| 情境 | 0x21 平均 | 最大 | P95 | FIFO 交易 RTT |
| --- | ---: | ---: | ---: | ---: |
| 無 FIFO | 201.08 ms | 201.57 ms | 201.57 ms | — |
| 空佇列 | 201.05 ms | 201.40 ms | 201.40 ms | 0.62 ms |
| 有事件 | 200.96 ms | 201.36 ms | 201.36 ms | 0x25 0.60 ms；0x37 0.19 ms |
| 0x37 timeout 復原後 | 201.23 ms | 201.34 ms | 201.34 ms | timeout 250 ms、測試退避 100 ms；斷線後首個 0x21 為 352.77 ms |

這些數字是單機 TCP loopback 數據，不能反映 Gateway 排隊、RS485 傳輸或現場控制器回覆時間；上線前仍須依前述流程量測實機。

## 最近門禁事件 Sensor

Node 02 的「最近門禁事件」Sensor 狀態為既有 SQLite 的事件總數。
`events` attribute 依 `id` 由新到舊提供最多 100 筆，每筆包含
`id`、`received_at`、`event_time_local`、`event_type`、`port`、`user_address`。
Dashboard Card 可使用 `events[0]` 顯示最新事件，並逐筆呈現列表。
`user_address` 是控制器使用者位址，姓名與實際通行結果需另有已確認資料才能呈現。

啟動及 FIFO 保存成功後自動透過 executor 更新。
可使用原生 `homeassistant.update_entity`，指定此 Sensor 的實際 Entity ID 手動重新整理。
`events` 透過 `unrecorded_attributes` 排除 Recorder 儲存，總數仍可記錄。
歷史明細從既有 `access_events` 資料表以唯讀連線查詢。
資料庫不存在時顯示 0 與空列表，讀取失敗時 Sensor 顯示不可用，下次更新可恢復。

## SOYAL Dashboard

深色 Lovelace 儀表板提供連線、常開控制、解鎖輸出、選填門磁及最近 100 筆 FIFO 事件。
安裝工具會辨識現有實體並建立 Dashboard，詳見 [Dashboard 安裝與更新](dashboard/README.md)。
