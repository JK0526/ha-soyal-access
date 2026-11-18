# SOYAL Access Control

規格 v1.0 · Stage 1 · Home Assistant domain `soyal_access`

目前交付標準 `0x7E` Binary Protocol 與本機模擬測試。Home Assistant 安裝與 HACS 發布屬後續階段。

## 執行測試

執行環境為 Python 3.11 以上，核心與測試使用 Python 標準函式庫。Coverage.py 僅用於量測。

```sh
python3 -m unittest discover -v
python3 -m coverage run --branch --source=custom_components.soyal_access -m unittest discover -v
python3 -m coverage report -m
```

## 通訊設計

- 每條模擬 RS485 匯流排共用一個 `BusClient`。同一個 asyncio 鎖涵蓋連線、送出、收訊及關閉。站號、loopback 位址、TCP 埠及逾時由呼叫端提供。
- 每筆交易開啟獨立 RAW TCP 連線。逾時、斷線或取消後由下一筆交易建立新連線，原請求只送一次。交易逾時自取得排程鎖起算，關閉連線另有相同長度的等待上限。
- 接收器保留分段資料，依 LEN 提取封包，驗證 XOR 與 SUM。忽略標頭前的雜訊。已找到標頭的候選封包若長度低於下限或 checksum 錯誤，當筆交易失敗。
- 同一交易要求回應目的節點為主機 `0x00`，且來源 ID 符合請求站號。無來源 ID 的 ACK/NACK 可離線辨識，但無法關聯到交易。資料回應由呼叫端指定允許的回應碼與資料最小長度，未知尾端位元組保持原樣。
- TCP 僅接受數字 loopback 位址，且只允許 `0x18` 空參數、`0x21 00 00` 與 `0x25` 空參數。門鎖及事件刪除封包只作離線編解碼測試。程式沒有背景輪詢或封包內容日誌。

## 驗收紀錄

Python 3.13.3、Coverage.py 7.10.6。16 項測試通過，核心程式行與分支合併覆蓋率 100%。

- 兩站共 14 筆完整現場請求 HEX 的編碼輸出逐 Byte 相符，解碼亦還原原始欄位。
- 三筆現場狀態前綴可用來核對來源 ID 與 Offset 6 Bit 6。測試分別確認原始前綴會等待後續資料，以及補成合成封包後可擷取對應位元。現場回應的完整 checksum 仍待取得。該位元代表主門解鎖輸出。
- 31 Bytes 與另一種長度的事件回應、含來源站號的交易 ACK/NACK 均以合成資料驗證。附件未附完整的現場事件、I/O 與 ACK/NACK 回應 HEX。
- 本機模擬涵蓋兩站交易序列化、訊息分段、合併、異站回應、逾時、斷線、校驗錯誤、取消、重連及關閉。現場 Gateway 與控制器未連線。

## Stage 2 驗收建議

驗收 Stage 1 後，以每條 Gateway 匯流排一筆 Config Entry 管理連線及多站設定，在 entry 載入時建立共用 client，為每站建立獨立 Device，卸載時等待 client 關閉。先取得兩站的完整封包，確認各命令的回應格式、無來源 ACK 的處理、TCP 重連後是否仍可能收到 RS485 遲到回應，以及現場 Gateway 的連線策略。維持 Hosting Polling 預設關閉，待獨立運作模式測試通過再評估啟用。門磁支援仍待驗證。

參考文件

- [SOYAL Controller Protocol 4.04](https://files.soyal.com.tw/EN/download/protocol/Protocol_881E_725Ev2_82xEv5%204V04.pdf)
- [Python asyncio Streams](https://docs.python.org/3/library/asyncio-stream.html)
- [Home Assistant Config Entries](https://developers.home-assistant.io/docs/config_entries_index/)
