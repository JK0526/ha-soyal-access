# SOYAL 通訊架構

以 SOYAL v0.5.1-beta.3 為行為基準。參考 [Coselig v1.6.4](https://github.com/JK0526/coselig-modbus-hub/tree/v1.6.4) 的 Hub 建立物件、Coordinator 負責輪詢、Transport 處理接收與排程。SOYAL 以控制等待鎖保留 FIFO 順序，背景工作遇忙略過當輪。每次明確的控制操作各自交易。

| 職責 | beta.3 | beta.1（0.6.0） |
| --- | --- | --- |
| Config Entry 與生命週期 | `__init__.py`、`GatewayManager` | `__init__.py`、`hub.py` 持有物件，協調載入、停止、重載 |
| 持續 TCP、RX、配對及排程 | `session.py` 與 `gateway.py` | `transport.py` 擁有單一接收工作、交易鎖及優先排程 |
| 站號與 Gateway 通訊狀態 | `gateway.py` | `coordinator.py` 執行 15 秒健康輪詢，維持 Node 02 背景查詢及 HA 狀態通知 |
| 門鎖控制及復原 | `gateway.py` | `control.py` 處理命令結果、持久化封鎖和管理員復原 |
| 事件處理 | `gateway.py` 預覽、`event_sync.py` FIFO、`event_store.py` SQLite | 離線預覽移至 `event_sync.py`，既有 FIFO 與 SQLite 沿用 |

控制從寫入待確認紀錄到保存交易結果均持有操作鎖。Transport 另外限制同時只有一筆在途交易。建連失敗發生於送出前。開始寫入 socket 即標記回應可能遲到，只有配對的有效回應才能正常結束交易。解碼失敗、逾時或斷線會交由呼叫端更新狀態。已送出控制的未決狀態跨重啟保留，唯讀查詢可能遲到時仍封鎖該站。連線恢復不會解除封鎖。

Config Entry、裝置和 Entity 使用原有識別。SQLite 路徑及 schema、Gateway 端點雜湊及 HA 持久化格式沿用。正式 Transport 的允許清單僅含原有健康與已啟用的控制指令。管理員復原需暫停同一 Gateway、檢查持久化紀錄、取得現場排空證據，再確認目標站號。
