# SOYAL Access Control

規格 v1.0 · Stage 1.1 · Home Assistant domain `soyal_access`

目前交付標準 `0x7E` Binary Protocol、本機模擬測試，以及 HACS 開發預覽版 `0.1.1`。

## HACS 下載測試

1. 開啟 HACS，從右上角選單選擇「自訂儲存庫」。
2. 輸入 `https://github.com/JK0526/ha-soyal-access`，類型選擇「整合」，再按「新增」。
3. 開啟 **SOYAL Access Control** 並按「下載」。若已下載，可由項目選單選擇「重新下載」，測試從預設分支取得檔案。
4. 下載後可確認 `custom_components/soyal_access/manifest.json` 的版本為 `0.1.1`。

本預覽版可測試下載及檔案更新。Home Assistant 的「新增整合」、設備與實體功能屬 Stage 2。現場通訊不在本預覽版的測試範圍。

## 執行測試

核心使用 Python 3.11 以上的標準函式庫。完整驗收使用 pytest、Ruff 與 Coverage.py。

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q
python3 -m ruff check
python3 -m ruff format --check
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m coverage run --branch --source=custom_components.soyal_access -m pytest -q
python3 -m coverage report -m
```

原始 RX 可選擇保留在 Git 忽略的 `.local-rx/field.hex`，依下表前七列排序，每行一筆完整 HEX。執行全套驗收時，可在 pytest 或 coverage 指令前加上 `SOYAL_FIELD_RX=.local-rx/field.hex`。測試資料只由本機讀取，沒有原始檔時只略過該項。含識別資料的現場擷取檔與測試輸出應留在受控環境。

## 通訊設計

- 每條模擬 RS485 匯流排共用一個 `BusClient`。同一個 asyncio 鎖涵蓋連線、送出、收訊及關閉。站號、loopback 位址、TCP 埠及逾時由呼叫端提供。
- 每筆交易開啟獨立 RAW TCP 連線。交易逾時從取得排程鎖起算，關閉連線另有相同長度的等待上限。送出後若逾時、斷線、校驗失敗或取消，且未取得匹配回應，該站的後續交易在同一 client 生命週期內一律拒絕。其他站可依序建立新連線，原請求不會重送。連線建立失敗時，原站仍可在之後發起新交易。
- 接收器保留分段資料，依 LEN 提取封包，驗證 XOR 與 SUM。忽略標頭前的雜訊。已找到標頭的候選封包若長度低於下限或 checksum 錯誤，當筆交易失敗。
- 同一交易要求回應目的節點為主機 `0x00`，且來源 ID 符合請求站號。缺少來源 ID 或來源為 0 的 ACK/NACK 可分類，交易排除這些回應。資料回應由呼叫端指定允許的回應碼與資料最小長度，未知尾端位元組保持原樣。
- TCP 僅接受數字 loopback 位址，且只允許 `0x18` 空參數、`0x21 00 00` 與 `0x25` 空參數。門鎖及事件刪除封包只作離線編解碼測試。程式沒有背景輪詢或封包內容日誌。

## 驗收紀錄

Python 3.13.3、pytest 9.0.0、Ruff 0.17.3、Coverage.py 7.10.6。啟用本機原始資料後，33 項測試及 55 個子測試通過，核心 147 個敘述、50 個分支全覆蓋。Ruff 檢查與格式檢查通過。

| RX 案例 | 完整長度 | 回應碼 | 來源 | 資料與結果 |
| --- | ---: | --- | --- | --- |
| I/O 待機 | 11 | `0x03` | 01 | 原始及匿名化通過 |
| I/O 解鎖 | 11 | `0x03` | 01 | 原始及匿名化通過 |
| I/O 待機 | 11 | `0x03` | 02 | 原始及匿名化通過 |
| I/O 解鎖 | 11 | `0x03` | 02 | 原始及匿名化通過 |
| 事件移除 ACK | 12 | `0x04` | 02 | 原始及匿名化通過 |
| 控制器參數查詢 NACK | 12 | `0x05` | 02 | 原始及匿名化通過 |
| RTC | 19 | `0x03` | 02 | 原始及匿名化通過 |
| 事件 | 31 | `0x0B` | 02 | 合成通過，待完整現場 HEX |
| Hosting | 38 | `0x09` | 02 | 合成通過，待完整現場 HEX |
| 人員資料 | 15 | `0x03` | 02 | 合成通過，待完整現場 HEX |

每筆驗證標頭、LEN、XOR、SUM、回應碼、來源及編解碼逐 Byte 往返。I/O 額外核對 Offset 6 Bit 6。TCP 模擬涵蓋正確、異站及未知來源的 ACK、NACK、一般資料與 Hosting 回應，以及重連後轉送遲到 ACK。所有網路測試限於本機 loopback，事件移除 ACK 僅離線驗證。Stage 1.1 可驗收已提供的七筆現場 RX 與模擬通訊；完整現場 RX 驗收仍待補齊表中三筆資料。

## TCP 策略與 Stage 2 建議

| 策略 | 取捨 |
| --- | --- |
| 每筆短連線 | 每筆使用獨立 socket 與緩衝，故障可針對當筆清理。事件批次每筆多一次 TCP 建連與關閉，成本隨交易數增加。TCP 關閉無法清除 Gateway 的 RS485 緩衝。 |
| 每條匯流排一條長連線 | 批次可重用 socket，省下每筆握手及關閉成本。需要處理閒置斷線、背景接收、無待處理交易時的遲到訊框，以及重連後的同步。效益與相容性需以 Gateway 實測確認。 |

- 建議在受控驗證證實 Gateway 支援長連線後，將長連線用作批次同步預設。以每條匯流排一個 client 與唯一收訊工作維持單一在途交易，依等待順序派送。資料回應驗證來源與預期碼，無法關聯的回應丟棄。
- 連線世代可以隔離舊 socket 的資料。若 Gateway 將舊的 RS485 回應送上新 socket，同站同格式回應缺少交易序號，僅靠來源及回應碼無法區分。保留該站的結果未明狀態，取得可驗證的 Gateway 排空或重置訊號才恢復；若要用靜默窗，先證實延遲上限與 Gateway 緩衝行為。重新建 client 本身無法排空 Gateway。
- Config Entry 由管理者明確輸入 Host、Port 與站號。驗證主機格式、埠號範圍及站號，避免同一匯流排被重複設定。設定驗證只做經核准的 TCP 連通檢查或確認無副作用的查詢。RAW TCP 部署於受控網段，維持 Hosting Polling 預設關閉。

參考文件

- [SOYAL Controller Protocol 4.04](https://files.soyal.com.tw/EN/download/protocol/Protocol_881E_725Ev2_82xEv5%204V04.pdf)
- [Python asyncio Lock](https://docs.python.org/3/library/asyncio-sync.html#asyncio.Lock)
- [Home Assistant Config Flow](https://contributors.home-assistant.io/core/integrations/config-flow/)
