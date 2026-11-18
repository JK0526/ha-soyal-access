# 版本紀錄

版本來源為 [manifest.json](custom_components/soyal_access/manifest.json)。Git Tag 使用 `v` 前綴。預發佈使用 Semantic Versioning 的 `-beta.N` 或 `-rc.N`，正式版使用不帶預發佈後綴的版本。

## v0.5.0-beta.1

- Gateway 使用共用持續 TCP Session，背景接收與單一在途交易排程，閒置斷線可偵測，監控週期重新連線。
- 控制停用時仍每站每輪讀取一次 0x21/00 00。每輪之間等待 15 秒，實際輪距含交易耗時。
- 監控與控制共用來源配對及持久化未決保護。監控回應可能與控制回應同格式，逾時後保留站別封鎖，重連僅恢復 TCP。
- 常開狀態仍為 Unknown。現有證據僅驗證 Hosting 回應的輸出位元，I/O Relay 定義與門磁仍待實機確認。
- 長連線經 loopback 驗證，現場 Gateway 相容性待驗收。

## v0.4.0-beta.2

- 使用 HA 原生布林選擇器設定門鎖控制，提交值須為真正的布林值。整合以儲存的 `true` 啟用控制，缺值或型別不符時維持停用。
- 「最近控制結果」和下載診斷可查看 `control_block_reason`，區分設定停用、未決交易和儲存錯誤。保留既有站號封鎖、Entity Registry 與 SQLite 資料。
- 新增從 HA 前端 HTTP 流程啟用、停用及再次啟用雙站控制的回歸測試，並以本機 TCP 模擬驗證雙站指令。
- 此版為預發佈。仍須在目標 HA 主機與現場 Gateway 完成驗收。

## v0.4.0-beta.1

- 修復 Config Flow 表單序列化錯誤。使用 HA 可序列化的區間驗證，維持逾時與重連冷卻的有效範圍並拒絕非有限數值。
- 首次將 HA UI Config Flow 打包至 GitHub。舊版 `0.1.1` 預覽套件缺少 `config_flow` 宣告及處理器。新增 HA 前端 HTTP 端點的新增、重複偵測、重新設定及卸載再載入回歸測試。
- 補充手動更新與故障診斷指引。升級需完整替換整合程式目錄並重啟 HA Core。
- 包含雙站控制、通訊與未決狀態、SQLite 事件保存、管理員人員對照與歷史查詢，以及最新保存事件摘要。
- 此版為預發佈。目標 HA 主機與現場 Gateway 的實機驗收仍待完成。事件 FIFO 僅在 loopback 模擬啟用。
