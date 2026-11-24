# SOYAL Access Control

`v0.8.0-beta.8` 支援設定單一 Gateway 所監控的門禁控制器站號。整合 domain 為 `soyal_access`。

## HACS 安裝或更新

1. 若 HACS 尚未有此套件，在自訂儲存庫新增 `https://github.com/JK0526/ha-soyal-access`，類型選 Integration。
2. 開啟 SOYAL Access Control 的下載或重新下載選單，指定 `v0.8.0-beta.8`，完成後重新啟動 Home Assistant。
3. 已有 SOYAL Config Entry 會沿用原設定。首次安裝從「設定 → 裝置與服務 → 新增整合」選 SOYAL Access Control，輸入 Host、TCP Port 與門禁控制器站號。

升級會保留同一 Config Entry、Gateway 設定及現有門禁與狀態實體 ID。FIFO 在背景自動執行，不新增 Home Assistant 實體。新的事件資料寫入 `<HA_CONFIG>/soyal_access_fifo/<entry_id>.sqlite3`；舊版 `<HA_CONFIG>/soyal_access/<entry_id>.sqlite3` 與歷史安全紀錄保持原狀，不會被開啟、覆寫或刪除。版本來源見 [manifest.json](custom_components/soyal_access/manifest.json)，歷史版本見 [CHANGELOG](CHANGELOG.md)。

## 設定門禁控制器站號

首次安裝或在整合選單選擇「重新設定」，輸入控制器已設定的整數站號（預設 2）。依 [SOYAL E 系列設定說明](https://www.soyal.com/faq.php?act=view&id=885)，直接連線的單站控制器範圍為 1–254。[SOYAL 協定說明](https://www.soyal.com/faq.php?act=view&id=859) 將 0 保留給匯流排主站，255 用於廣播。每筆 Config Entry 以 Host＋Port 識別，維持一條 TCP Session、一個指定站號、200 ms 狀態輪詢及 2 秒 FIFO 檢查週期。不同端點可各自建立整合。

既有未設定站號的整合仍使用 Node 02，保留 Config Entry、Device、Entity 識別及原有超時 Store。其他站號各自保存常開超時設定與工作階段。站號變更會重載整合、回收舊連線與計時工作、清除舊站號的有效 Entity／Device 登錄，建立新站號自己的實體。若有在途控制、未結束或未確認的常開工作階段（包含保護關閉時仍在常開），先確認現場並成功解除常開，再切換站號。切換本身不會發送門禁控制。

FIFO 狀態回覆仍嚴格比對原有固定欄位，只有來源站號改為目前設定，I/O 位元組依原規則略過。非 Node 02 的實際通訊與各韌體 FIFO 固定欄位相容性待實機確認。

### beta.8 實機驗收

1. 由 HACS 更新 beta.8 後重啟，確認現有 Node 02 的 Entity ID、超時設定與歷史保留。
2. 在可監看門鎖的環境確認舊站號已解除常開，再設定目標站號。確認舊控制實體移除、新裝置名稱正確，且只有配置的 Gateway 連線。
3. 等待有效輪詢後驗證在線、解鎖輸出、單次開門、常開與解除。確認 FIFO 事件與 HA 操作都只出現在目前站號的歷史。
4. 在常開中嘗試切換，確認遭阻擋。手動解除後再切換，確認新站號使用自己的超時設定且不承接舊計時。

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
| `hub.py` | 每筆 Config Entry 持有一個 Transport 與常開超時管理器 |
| `hold_open_timeout.py` | 原生 Store、常開工作階段、絕對期限與單次排程 |
| `switch.py`、`number.py` | 常開命令狀態與兩項原生超時設定 |
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

## v0.8.0-beta.6 常開超時保護

Node 02 裝置新增兩個 HA 原生設定實體，不需 Helper、Automation 或自訂前端。Gateway 離線時仍可調整設定，調整本身不會送出開門或解除常開指令；重新啟用已到期的已確認工作階段則會進入解除流程。

| 實體 | 預設與範圍 | Unique ID |
| --- | --- | --- |
| 常開超時保護 / Hold Open Timeout Protection | ON；CONFIG；`mdi:shield-check` | `<entry_id>:2:hold_open_timeout_protection` |
| 常開時間上限 / Hold Open Timeout | 30 min；1–1440；整數步進 1；BOX；CONFIG；`mdi:timer-cog-outline` | `<entry_id>:2:hold_open_timeout` |

點選「常開時間上限」開啟更多資訊，以輸入框設定分鐘數，或呼叫原生 `number.set_value`。Entity ID 由 HA 裝置名稱、語言及使用者既有命名決定，請從 Node 02 裝置頁確認。既有常開 Switch 的 Entity ID、Unique ID、assumed state 與控制指令語意保留。

### 計時與設定變更

- 常開收到既有 Transport 驗證的成功回覆後才記錄 UTC 開始時間與絕對截止時間。脈衝開門不啟動或延長計時，常開失敗不建立工作階段。
- 重複成功執行常開沿用原始開始時間，不延長期限。手動解除成功才清除工作階段；NACK 保留原有保護。
- 常開中修改分鐘數，以原始開始時間重算期限。新期限已過時立即進入待解除流程。
- 關閉保護取消未送出的排程，保留已確認的開始時間，不改變控制器狀態。重新啟用依原始時間恢復；沒有已確認工作階段時不推測常開，也不從短暫 unlock output ON 推定常開。
- 停用或改設定無法撤回已送出的交易；交易仍依真實結果結束。手動控制與超時解除共用 Hub 的 busy 與交易鎖。

### 持久化、重啟與異常

每筆 Config Entry 以 HA 原生 Store 獨立保存至 `.storage/soyal_access.<entry_id>.hold_open_timeout`，使用 atomic write，設定與工作階段時間皆為 timezone-aware UTC。成功常開建立的原始期限會在 HA Restart / Integration Reload 後恢復剩餘時間，**不重新給予完整分鐘數，不自動執行常開**。常開 Switch 載入後仍從 Unknown 開始；恢復的工作階段只是之前成功交易的紀錄，不等於控制器或電鎖實體回授。

期限已過但 Gateway / Node 離線時保持 `expired_pending`。Transport 完成原有靜默觀察、兩筆有效輪詢驗證且允許控制後，才透過既有 Hub 流程解除；未送出的暫時不可用可等待後續連線驗證，不使用高頻重試。NACK 標記 `failed` 並警告；已送出但逾時、斷線、無效回應或取消則標記 `uncertain`，不自動重送。切換保護或修改分鐘數不會清除 `failed` / `uncertain`。確認現場狀態及舊回覆去向後，明確手動解除成功才能清除該工作階段；再手動常開可建立新工作階段。

每次常開 / 解除常開先完成一筆 `uncertain` 寫入，再送原有控制指令，收到確認後才保存結果。因此控制成功與最後保存之間若當機，恢復會保守標記不確定，不將中斷交易還原成成功或重播指令。日誌會警告磁碟保存失敗，`timeout_restore_safe: false` 表示最近保存或載入未成功。交易前的 journal 寫入失敗會在送出前拒絕常開 / 解除操作；脈衝開門與既有背景輪詢不依賴此 journal。交易後保存失敗時，本次記憶體中的已確認計時仍繼續，但不能保證重啟恢復。這兩筆小型非同步儲存會增加常開控制的本機磁碟等待，沒有額外網路往返；實機延遲須驗收。

Store 的少量相容性包裝會檢查 HA 內部實際寫入完成，避免 Store 吞掉 WriteError 後被當成成功；升級 HA 時應執行磁碟失敗及關機保存測試。檔案 I/O 仍由 HA executor 執行，未使用 SQLite 或更動其 schema。

**HA 主機關機、整合卸載或無法連線期間，無法保證在期限當下解除常開。** 此功能不能取代控制器本身的硬體安全機制。持久化也不能解決 SOYAL 缺少交易識別碼造成的舊回應歸屬限制。

### 常開 Switch attributes

| Attribute | 說明 |
| --- | --- |
| `timeout_enabled`、`timeout_minutes` | 目前保護設定 |
| `timeout_active` | 已啟用且正在計時、等待解除或送出解除 |
| `hold_open_started_at`、`hold_open_deadline` | 原始 UTC 開始及截止時間；無工作階段為 null |
| `timeout_status` | `idle`、`disabled`、`active`、`expired_pending`、`releasing`、`failed`、`uncertain` |
| `timeout_restore_safe` | 最近儲存 / 載入是否成功；不表示實體鎖狀態已確認 |

正常流程：`idle → active → expired_pending → releasing → idle`。停用顯示 `disabled`；失敗 / 不確定狀態優先保留。超時資訊僅在設定、狀態或期限改變時發布，不每秒更新倒數，因此不增加每秒 Recorder 寫入。

### 原生 Tile Card Dashboard

將下列三個 Entity ID 換成 Node 02 裝置頁上的實際 ID，再貼到 Dashboard 的手動卡片。時間卡點選後使用 HA 原生 BOX 輸入框；未加入 Slider feature。

```yaml
type: vertical-stack
cards:
  - type: tile
    entity: switch.soyal_ar_725e_node_02_hold_open
    name: 常開
    tap_action:
      action: more-info
    icon_tap_action:
      action: toggle
  - type: tile
    entity: switch.soyal_ar_725e_node_02_hold_open_timeout_protection
    name: 常開超時保護
    tap_action:
      action: more-info
    icon_tap_action:
      action: toggle
  - type: tile
    entity: number.soyal_ar_725e_node_02_hold_open_timeout
    name: 常開時間上限（分鐘）
    tap_action:
      action: more-info
```

### 現場實機驗收（beta.6 已由使用者通過）

1. HACS 選取 `v0.8.0-beta.6` Pre-release 並重啟；確認版本、既有 Entity ID、FIFO 歷史與 Node 02 兩個新設定，預設 ON / 30 min，時間輸入為 BOX。
2. 於可監看電鎖的受控時段，設為 1 分鐘後常開。記錄 started_at / deadline，確認成功後才開始計時；脈衝開門與再次常開不延長期限。到期只解除一次，現場確認電鎖恢復。
3. 手動解除成功後確認計時取消。先設 2 分鐘、常開後等待一段時間，改為 1 分鐘，確認截止仍以原始開始時間計算；若已過期應進入解除。
4. 常開期間停用保護，確認不立即改變電鎖；超過原期限後重新啟用，確認等待連線驗證後解除。無已確認工作階段時啟用，確認不送控制指令。
5. 設為 2 分鐘並常開；期限前 Reload / Restart，確認原期限保留、Switch 仍為 assumed state，不自行常開。另測 HA 停機跨過期限，啟動且兩筆有效輪詢後解除，不補給完整時間。
6. 常開後讓 Gateway 離線跨過期限，確認 `expired_pending`；恢復後先完成既有驗證再解除。觀察 TCP、200 ms 輪詢及 FIFO 同步，記錄控制回應、輪詢 P95 / 最大間隔與 journal 儲存延遲。
7. 在可安全注入錯誤的測試環境驗證 NACK 為 `failed`、已送出結果不明為 `uncertain`，重連、Reload、切換設定皆不自動重送。確認現場與舊回應去向後再明確手動解除。
8. Unload / Reload / HA Stop 後檢查舊 timer / task / socket 均收回；確認重載不重複解除。以測試用唯讀 / 滿磁碟環境驗證保存警告及 `timeout_restore_safe: false`，避免在正式門禁主機上直接製造磁碟故障。

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

「最近門禁事件」Sensor 僅統計目前站號的歷史，舊站號的資料留在原 SQLite。
`events` attribute 依 `id` 由新到舊提供最多 100 筆，每筆包含
`id`、`node_id`、`received_at`、`event_time_local`、`event_type`、`port`、`user_address`、
`source`、`ha_user_id`、`ha_user_name`。
以下內建 Markdown 卡片範例顯示最新 15 筆事件。未知事件不推斷通行結果；缺少欄位時顯示「未知」。
`user_address` 是控制器使用者位址，姓名與實際通行結果需另有已確認資料才能呈現。

啟動及 FIFO / HA 操作保存成功後自動透過 executor 更新。
可使用原生 `homeassistant.update_entity`，指定此 Sensor 的實際 Entity ID 手動重新整理。
`events` 透過 `unrecorded_attributes` 排除 Recorder 儲存，總數仍可記錄。
歷史明細從既有 `access_events` 資料表以唯讀連線查詢。既有 SQL 可用 `WHERE node_id = 5` 篩選指定站號，例如 `SELECT * FROM access_events WHERE node_id = 5 ORDER BY id DESC LIMIT 100;`。
資料庫不存在時顯示 0 與空列表，讀取失敗時 Sensor 顯示不可用，下次更新可恢復。

### v0.8.0-beta.7 HA 操作歷史

僅記錄 Hub `outcome == "success"` 的控制器接受結果，**不代表實體門磁、鎖舌或實際通行回授**。不記錄 NACK、未送出、結果不明或取消的交易，也不記錄保護設定變更、背景輪詢及 FIFO 內部操作。

| event_type | 顯示 | source | 操作者 |
| --- | --- | --- | --- |
| `ha_pulse_open` | 📱 HA 單次開門 | `ha` | 當次 HA Context 使用者 |
| `ha_hold_open` | 🔓 HA 常開模式 | `ha` | 當次 HA Context 使用者 |
| `ha_release_open` | 🔒 HA 解除常開 | `ha` | 當次 HA Context 使用者 |
| `ha_hold_open_timeout` | ⏱️ 常開超時解除 | `ha` | ID 為 NULL，名稱為「系統」 |

Button / Switch 在操作開始時擷取 Context 的 `user_id`，成功後透過 HA 原生 auth API 查詢並保存當次使用者名稱；沒有 Context user_id 或無法識別使用者時，兩個使用者欄位保留 NULL，顯示「未知來源」，不推測為自動化。名稱為當次操作的快照，後續改名不會回寫歷史。每次最多查詢一次，不另建快取。

沿用 `<HA_CONFIG>/soyal_access_fifo/<entry_id>.sqlite3` 的 `access_events`，初始化時使用 `PRAGMA table_info` 檢查，再對缺少欄位執行 `ALTER TABLE ADD COLUMN`：

```sql
source TEXT NOT NULL DEFAULT 'soyal'
ha_user_id TEXT DEFAULT NULL
ha_user_name TEXT DEFAULT NULL
```

升級可重複執行，保留既有資料與流水號；新舊安裝 schema 一致，舊 FIFO 事件及後續 FIFO 寫入的 source 均為 `soyal`。既有欄位與 SQL 查詢仍可使用；需要只看原生事件時加上 `WHERE source = 'soyal'`。HA 事件的 `node_id` 為當次實際控制目標；`event_code`、`port`、`user_address` 使用 `-1`，`raw_frame_hex` 使用空字串，不偽造 SOYAL 封包或人員編號。

成功回覆時間先以 UTC 記錄，`received_at` 沿用 UTC ISO 格式；HA `event_time_local` 為相同時刻的 HA 本地時間，包含時區。完成原有命令狀態與超時管理、釋放交易鎖及 busy 後，才查詢使用者並透過 HA executor 寫入 SQLite，成功後沿用 `events_signal` 更新 Sensor。歷史儲存失敗只警告，不將控制回報為失敗，不重送、不觸發 FIFO 刪除；不建立常駐歷史工作或背景任務佇列。操作呼叫會等待這筆歷史處理結束，但此時其他控制已可執行。

歷史屬於盡力保存；控制器接受指令後若 HA 中斷或資料庫寫入失敗，可能缺少該筆紀錄，不會為補記歷史重播控制。Sensor 總數包含兩種來源，仍只輸出最新 100 筆、依 id DESC 排序，`events` 不進入 Recorder，保留 `invalid_user_pin` 相容處理與既有 Entity ID / Unique ID。

### 內建 Markdown 卡片

編輯現有 Markdown 卡片並貼上下列 YAML，將 `sensor.node_02_access_event_history` 換成實際 Entity ID。最新 15 筆包含兩種來源，SOYAL 事件沿用原本的人員編號顯示：

```yaml
type: markdown
title: 最近門禁事件
content: |-
  {% set entity = 'sensor.node_02_access_event_history' %}
  {% set events = state_attr(entity, 'events') or [] %}
  {% set ha_labels = {
    'ha_pulse_open': '📱 HA 單次開門',
    'ha_hold_open': '🔓 HA 常開模式',
    'ha_release_open': '🔒 HA 解除常開',
    'ha_hold_open_timeout': '⏱️ 常開超時解除'
  } %}
  {% if not has_value(entity) %}
  歷史資料目前無法讀取。
  {% elif events | count == 0 %}
  尚無門禁事件。
  {% else %}
  {% for event in events[:15] %}
  {% set kind = event.get('event_type', 'unknown_event') %}
  {% if event.get('source') == 'ha' and kind in ha_labels %}
  **{{ event.get('event_time_local') or event.get('received_at') or '時間未知' }}** · {{ ha_labels[kind] }} · {{ event.get('ha_user_name') or '未知來源' }}
  {% else %}
  **{{ event.get('event_time_local') or event.get('received_at') or '時間未知' }}** · {% if kind == 'card_access' %}卡片事件{% elif kind == 'egress' %}出門事件{% elif kind == 'pin_access' %}密碼事件{% else %}未知事件{% endif %} · 使用者位址：{{ event.get('user_address', '未知') }} · 門號：{{ event.get('port', '未知') }}
  {% endif %}
  {% endfor %}
  {% endif %}
```

### beta.7 簡短實機驗收

1. HACS 選取 beta.7 並重啟，確認舊事件、流水號、Sensor ID 與常開保護設定保留。
2. 從已登入的 HA 分別執行單次開門、常開、解除常開，確認每次成功只增加一筆對應紀錄、名稱與本地時間正確，並人工核對電鎖。
3. 設常開上限為 1 分鐘，確認自動解除記為 `ha_hold_open_timeout` / 系統，原 beta.6 行為保持正常。
4. 核對來源不明操作顯示「未知來源」，FIFO 事件及人員編號顯示正常；在受控測試環境確認失敗指令不增加 HA 成功紀錄。
5. 重載整合，確認混合歷史保留、最新 100 筆上限與 Markdown 卡片更新正常。
