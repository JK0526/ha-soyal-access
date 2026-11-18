# SOYAL Access Control

規格 v1.0 · Stage 5A · Home Assistant domain `soyal_access`

目前為 Beta 測試版。版本以 [manifest.json](custom_components/soyal_access/manifest.json) 為準，Git 標籤為 manifest 版本加上 `v`。版本紀錄見 [CHANGELOG](CHANGELOG.md)。

若 HA 提示只能使用 YAML，先確認已安裝目錄同時包含帶有 `"config_flow": true` 的 `manifest.json` 與 `config_flow.py`。舊版 `0.1.1` 預覽套件缺少這兩項。從對應 GitHub Release 下載 Source code，解壓後完整替換 HA 設定目錄的 `custom_components/soyal_access`，保留 HA 設定與資料庫，然後重啟 HA Core。已有 Gateway 可使用「重新設定」，新安裝可用「新增整合」。若仍失敗，記錄 HA Core 版本、已安裝 manifest 版本，以及 SOYAL、loader 或 Config Flow 相關錯誤日誌。

[Stage 5A 安裝步驟與雙站驗收表](docs/stage-5a-acceptance.md)

## Home Assistant 設定與操作

1. 安裝此版本的 `custom_components/soyal_access` 到測試 HA，重新啟動後從「裝置與服務」新增 **SOYAL Access Control**。
2. 設定 Gateway 的 Host、TCP 埠（預設 502）、RAW TCP 模式、站號（預設 1,2）、交易逾時及重連冷卻。使用同一個規範化 Host 與埠代表同一條匯流排。同一台設備只能使用一種端點表示，避免另外用 DNS 別名、IP 或其他 TCP 埠加入同一匯流排。
3. 新增與重新設定只檢查輸入格式。預設停用門鎖控制。啟用後，只有明確呼叫 Button 或 Switch 動作才會建立 TCP 連線；啟動、重載和卸載本身不送控制命令，也不執行任何健康輪詢。
4. 各站有脈衝開門 Button、常開 Switch、最近通訊狀態、最近有效回應時間、錯誤數、不確定狀態及最後控制結果。Switch 採 `assumed_state`，讀值維持 Unknown，因為目前沒有可信的持續實體回授。匹配的控制 ACK 或 `0x03` 資料回應表示交易已完成，實體鎖舌狀態未由整合感測。
5. Gateway Device 提供目前 TCP socket 狀態、排隊數與在途交易屬性、匯流排通訊錯誤數。短連線的 idle 狀態顯示 TCP 未連線。各站的最近通訊狀態初始為 Unknown，計數及時間由本次載入起算。NACK 算有效回應並標記控制遭拒，通訊錯誤計數另計。

每個 Gateway 對應一個 Config Entry 和共用 Manager。站號 Device 及 Entity 的識別碼由 entry ID、站號與實體類別組成，重新排序或增減站號時，其餘 Device 和 Entity 的識別碼不變。重新設定方式為整合項目選單的「重新設定」。

## 執行測試

通訊核心測試可獨立執行。HA 測試使用 Python 3.13、Home Assistant 2026.2.3 與 pytest-homeassistant-custom-component 0.13.316。使用兩次 pytest 執行以隔離 HA 測試外掛的事件迴圈與 socket 限制。

```sh
SOYAL_FIELD_RX=.local-rx/field.hex SOYAL_FINAL_RX=.local-rx/final.hex \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m coverage run --branch --source=custom_components.soyal_access \
  -m pytest -p pytest_asyncio.plugin -q tests/test_client.py tests/test_protocol.py tests/test_rx.py tests/test_fifo.py tests/test_history.py
SOYAL_CONTROL_RX=.local-rx/control.hex python3 -m coverage run --append --branch --source=custom_components.soyal_access -m pytest -q tests/test_ha.py
python3 -m coverage report -m
python3 -m ruff check
python3 -m ruff format --check
docker run --platform linux/amd64 --rm -v "$PWD:/github/workspace:ro" ghcr.io/home-assistant/hassfest
```

兩批原始 RX 可分別依下表前七列及後六列順序保留在 Git 忽略的 `.local-rx/field.hex`、`.local-rx/final.hex`。未指定原始檔時，只略過對應的原始資料測試。控制 RX 可另保留於 Git 忽略的 `.local-rx/control.hex`，每行一筆，依脈衝開門、常開、解除常開排序，每組先站 01 再站 02。未設定 `SOYAL_CONTROL_RX` 時，HA 測試仍會重播匿名化控制回應。含識別資料的現場擷取檔與測試輸出應留在受控環境。

## 通訊設計

- 每條模擬 RS485 匯流排共用一個 `BusClient`。同一個 asyncio 鎖涵蓋連線、送出、收訊及關閉。站號、loopback 位址、TCP 埠及逾時由呼叫端提供。
- 每筆交易開啟獨立 RAW TCP 連線。交易逾時從取得排程鎖起算，關閉連線另有相同長度的等待上限。送出後若逾時、斷線、校驗失敗或取消，且未取得匹配回應，該站的後續交易在同一 client 生命週期內一律拒絕。其他站可依序建立新連線，原請求不會重送。連線建立失敗時，原站仍可在之後發起新交易。
- 接收器保留分段資料，依 LEN 提取封包，驗證 XOR 與 SUM。忽略標頭前的雜訊。已找到標頭的候選封包若長度低於下限或 checksum 錯誤，當筆交易失敗。
- 同一交易要求回應目的節點為主機 `0x00`，且來源 ID 符合請求站號。缺少來源 ID 或來源為 0 的 ACK/NACK 可分類，交易排除這些回應。資料回應由呼叫端指定允許的回應碼與資料最小長度，未知尾端位元組保持原樣。
- Stage 1 預設 transport 僅接受數字 loopback 位址，且只允許 `0x18` 空參數、`0x21 00 00` 與 `0x25` 空參數。Stage 2 已明確啟用控制的 Manager 使用獨立 allowlist，只允許三種門鎖控制命令。程式沒有背景輪詢或封包內容日誌。

## 驗收紀錄

Python 3.13.3、pytest 9.0.0、Ruff 0.17.3、Coverage.py 7.10.6。39 項 Stage 1 測試與 16 項 HA 測試通過，另有 55 個子測試。傳輸與協定核心覆蓋率 100%，整合整體 97%。Ruff 檢查、格式檢查及 hassfest 通過。

| RX 案例 | 完整長度 | 回應碼 | 來源 | 資料與結果 |
| --- | ---: | --- | --- | --- |
| I/O 待機 | 11 | `0x03` | 01 | 原始及匿名化通過 |
| I/O 解鎖 | 11 | `0x03` | 01 | 原始及匿名化通過 |
| I/O 待機 | 11 | `0x03` | 02 | 原始及匿名化通過 |
| I/O 解鎖 | 11 | `0x03` | 02 | 原始及匿名化通過 |
| 事件移除 ACK | 12 | `0x04` | 02 | 原始及匿名化通過 |
| 控制器參數查詢 NACK | 12 | `0x05` | 02 | 原始及匿名化通過 |
| RTC | 19 | `0x03` | 02 | 原始及匿名化通過 |
| Egress 事件 | 31 | `0x10` | 02 | 原始及匿名化通過 |
| 正常刷卡事件 | 31 | `0x0B` | 01 | 原始及匿名化通過 |
| Hosting 正常上鎖 | 38 | `0x09` | 02 | 原始及匿名化通過 |
| Hosting 常開解鎖 | 38 | `0x09` | 02 | 原始及匿名化通過 |
| 人員資料 A | 15 | `0x03` | 02 | 原始及匿名化通過 |
| 人員資料 B | 15 | `0x03` | 02 | 原始及匿名化通過 |

每筆驗證標頭、LEN、XOR、SUM、回應碼、來源、逐 Byte 往返及每個分包位置。Hosting 額外核對 Offset 6 Bit 6 與所有內嵌事件位元組。Loopback TCP 測試使用匿名化資料驗證不同長度回應混合分包與黏包。全部 13 筆原始 RX 通過離線驗收。

## Stage 2 TCP 策略

| 策略 | 取捨 |
| --- | --- |
| 每筆短連線 | 每筆使用獨立 socket 與緩衝，故障可針對當筆清理。事件批次每筆多一次 TCP 建連與關閉，成本隨交易數增加。TCP 關閉無法清除 Gateway 的 RS485 緩衝。 |
| 每條匯流排一條長連線 | 批次可重用 socket，省下每筆握手及關閉成本。需要處理閒置斷線、背景接收、無待處理交易時的遲到訊框，以及重連後的同步。效益與相容性需以 Gateway 實測確認。 |

Stage 2 選擇短連線，沿用已驗證的分包接收器與每筆獨立緩衝。同一匯流排以單一 Manager 的 FIFO 等待鎖限制同時最多一筆交易。單一交易由自己持有的讀取工作收訊，控制交易接受來源匹配的 `0x04` ACK 或 `0x03` 11 Bytes I/O 資料回應；來源匹配的 `0x05` NACK 判為拒絕。其他資料碼或資料長度無法確認控制結果。斷線後依設定拒絕冷卻期間的請求；冷卻結束後，只接受新的明確操作。短連線避免引入背景收訊、閒置重連及多世代 buffer 管理，未來可於受控 Gateway 驗證完成後評估長連線的批次效益。

### 不確定狀態與管理員恢復

- 每次送出控制之前，先以 HA 原子寫入保存對應端點與站號的待確認記號。收到匹配的 ACK、NACK 或符合控制格式的資料回應，或確認連線失敗發生於寫入 socket 前，才可清除記號。已送出但遇到逾時、斷線、校驗錯誤或取消時，站號保持封鎖；重載和重啟不會解除。儲存資料無法辨識時，該 Gateway 的所有站號保持封鎖。
- 碰到 `uncertain` 時，管理員先停用控制並暫停相關自動化，記下受影響站號與原操作，依現場紀錄或獨立方式確認原命令結果。取得 Gateway 廠商確認的緩衝清除或重置程序後，在受控時段以實測證明舊 RS485 回應不能再送出；重開 TCP 或等待任意秒數不足以證明。
- 目前沒有強制解封按鈕。安全恢復的管理流程是經驗證排空後，以管理員維護程序停用該整合、備份並審核對應的 HA 待確認資料，只移除已確認的站號記號，最後重新載入並手動啟用控制。端點資料位於 HA 設定目錄下 `.storage/soyal_access.<endpoint-hash>`，雜湊來自規範化 Host、Port 的 Python tuple 表示。若無法確認原結果、儲存資料完整性或 Gateway 緩衝狀態，保留封鎖並交由維護人員處理。切換 Host 別名或移除儲存檔不應作為恢復方法。
- 有副作用的命令逾時不自動重送。Stage 3 前須實測 Gateway 緩衝邊界、控制 ACK 來源欄位與 Firmware 時序，並為事件刪除建立持久化的讀取、保存、確認及安全恢復流程。常開或刪除是否已生效必須由可驗證的結果判定。

## Stage 3 事件保存與離線同步

資料庫位於 HA 設定目錄的 `soyal_access/<config_entry_id>.sqlite3`。每個 Gateway 有獨立資料庫，以單一工作執行緒執行 SQLite。寫入用 `BEGIN IMMEDIATE`、明確 COMMIT 與錯誤回滾。使用 DELETE journal、FULL synchronous、5 秒 busy timeout 及外鍵。卸載關閉連線，歷史資料永久保留。

| 資料表 | 內容 |
| --- | --- |
| `access_events` | 本地 ID、Gateway、站號、控制器時間、接收與更新時間、事件類型、人員位址、Port、原始 BLOB、SHA-256、歷史來源與確認狀態 |
| `sync_state` | 每站階段、當前事件、原始佇列頭、刪除意圖、可能已送出、保存與確認筆數、最近成功時間及錯誤 |
| `schema_migrations` | SQLite schema 版本與套用時間 |

控制器時間按原始日曆欄位保存為無時區的本地時間，UTC 接收時間獨立保存。事件摘要供比對，資料表以本地 ID 區分事件。Hosting 的內嵌事件保留在通訊層。所有補讀標記為 `backfill`，即時事件的分界待確認控制器空佇列格式後建立。

同步採站別 FIFO。每次讀取驗證來源、事件碼、長度與校驗值。事件與佇列頭先提交為 `ready_to_delete`，再提交刪除意圖與 `possibly_sent=1` 進入 `delete_in_flight`。收到匹配 ACK，且重新讀取取得不同的有效佇列頭後，才將舊事件標記為確認並保存新檢查點。拒絕、斷線、未知回應、相同佇列頭或檢查點不符均停止該站。重載從已確認檢查點重新核對，未完成的刪除流程保留為 `uncertain`，供人工核對。ACK 遺失後保留封鎖，沒有自動重送。這個流程有持久化檢查點，仍可能遇到內容完全相同且無法辨識的合法事件。

部署時同步停用。離線模擬需要明確開啟 loopback FIFO 模式，並另行開啟刪除。批次逐筆處理，預設每批最多 16 筆、筆間間隔 0.1 秒。同一匯流排的控制操作可在同步交易之間排隊。診斷提供各站保存及確認筆數、速率、狀態與安全錯誤分類。總事件數未知。模擬以明確批次上限結束並提供後續佇列頭，空佇列格式仍待實機確認。

HA Core 備份期間暫停事件資料庫作業。離線驗證實際 Core 封存包含 SQLite 資料庫，即使排除 Recorder，事件與同步狀態均可讀回。SQLite 線上備份使用 backup API。還原審查會寫入新檔並暫停快照內的同步記錄。若使用 HA 的完整還原，先比對所有現場佇列與還原檢查點，再評估是否能安全恢復同步。Stage 4 可在此基礎上加入唯讀歷史查詢、人員對照與 CSV；啟用即時事件與刪除前須先驗證空佇列、重複事件識別與 Gateway 遲到回應。

## Stage 4 MVP 人員對照與歷史

每站的「最近已保存事件」Sensor 顯示事件類型，屬性提供控制器原始時間、UTC 匯入時間、事件來源與同步階段。此實體定期讀取本機 SQLite。含姓名、員工編號與 User Address 的完整內容透過管理員 WebSocket 查詢。歷史補讀只更新已保存紀錄的狀態，整合沒有發送即時通行事件。

SQLite schema v2 在原有交易中新增 `people` 表與歷史索引。每筆 Config Entry 沿用獨立資料庫，人員以站號和 User Address 識別。遷移在失敗時回滾，已有事件的原始 BLOB 保留。備份與審查還原支援 v1、v2。

用 HA 管理員身分連線至 `/api/websocket`。每項命令都需提供唯一的請求 `id` 和對應的 `entry_id`。

| type | 參數與用途 |
| --- | --- |
| `soyal_access/events` | 可選 `filters`，包含 `start`、`end`（含首尾的控制器日曆日期）、`station`、`user_address`、`name`（子字串）、`event_type`（數字）。省略篩選並指定 `limit: 1` 可查最新完整紀錄。 |
| `soyal_access/people` | 可選 `filters`，包含 `station`、`user_address`、`name`。 |
| `soyal_access/person/save` | `action` 為 `create` 或 `update`，同時提供 `station`、`user_address`、`name`。可選 `employee_id`。以 `update` 省略員工編號會清空該欄。 |
| `soyal_access/person/delete` | 提供 `station` 和 `user_address` 以移除本地對照。 |

清單回傳 `rows` 與 `next_cursor`。每頁預設 50 筆、上限 200 筆。下一頁維持相同篩選，將 `next_cursor` 傳入 `cursor`。歷史依控制器時間及本地 ID 由新至舊排序。未知事件碼保留為原始數字。查詢以當前姓名表對照歷史，未建對照時 `display_name` 使用 User Address。人員編號重新分配後，舊事件的顯示姓名可能改變，原始事件與人員位址仍可供人工核對。歷史姓名可信度以「目前對照」理解。

歷史與人員的圖形管理介面留待驗收後決定。本階段交付 HA 原生摘要與可測試的管理員後端。

參考文件

- [SOYAL Controller Protocol 4.04](https://files.soyal.com.tw/EN/download/protocol/Protocol_881E_725Ev2_82xEv5%204V04.pdf)
- [Python asyncio Lock](https://docs.python.org/3/library/asyncio-sync.html#asyncio.Lock)
- [Home Assistant Config Flow](https://contributors.home-assistant.io/core/integrations/config-flow/)
