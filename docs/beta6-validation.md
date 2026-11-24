# v0.8.0-beta.6 離線驗證紀錄

日期：2026-11-24（Asia/Taipei）。基準 tag：`v0.8.0-beta.5` / `cc750b8124ee752751351deef6f9d3fe859f0790`。開發分支：`feature/access-event-history`。

## 驗證環境與執行方式

- Python 3.14.7、Home Assistant 2026.10.4、pytest 9.0.3、pytest-homeassistant-custom-component 0.13.375。
- Ruff 0.17.3。
- Hassfest 使用本地 Home Assistant Core checkout `f93f45674efaa8db1c41dcd8ee0a7e82e015f768` 與 HA 2026.11.1 環境。
- HA 2026.11.1 環境與現有 pytest 外掛的 logger API 不相容，未宣稱完整 pytest 已在 2026.11.1 通過；另以未使用 pytest 外掛的程式驗證該版實際 Store 寫入、WriteError 偵測與停止時落盤，皆通過。

```sh
# 先載入 HA，避免現有 pytest 外掛載入順序引起 httpx alias 衝突。
# 兩個 SOYAL_* 路徑是本地既有封包，不隨版本發布；缺少時兩項測試會略過。
SOYAL_FIELD_RX=.local-rx/field.hex SOYAL_FINAL_RX=.local-rx/final.hex \
  .venv-ha-2026.10.4/bin/python -c 'import homeassistant; import pytest; raise SystemExit(pytest.main(["-q", "--disable-warnings"]))'
ruff check custom_components tests
ruff format --check custom_components tests
git diff --check

# 指定本地官方 Core checkout；不修改 Core。
PYTHONPATH=/tmp/ha-core-hassfest/packages/hassfest:/tmp/ha-core-hassfest \
  .venv-ha-2026.11.1/bin/python -m script.hassfest \
  --core-path /tmp/ha-core-hassfest \
  --integration-path "$PWD/custom_components/soyal_access" --action validate
```

## 結果

- 新增 timeout 測試：35 passed。使用模擬 clock、HA 原生 timer 與模擬時間，不實際等待 30 分鐘。
- 完整回歸（含本地封包）：145 passed、35 subtests passed，70.55 秒，無失敗、無略過。
- Ruff、Ruff format check、git diff check：通過。
- Hassfest：1 integration，0 invalid integrations。
- README YAML 解析、manifest 版本、strings / 英文翻譯一致性：通過。
- 與 beta.5 比較，Transport、protocol、FIFO 解析 / SQLite、歷史 Sensor、Config Flow 檔案完全相同；Hub 輪詢、FIFO、驗證與脈衝方法 AST 相同。

## 測試涵蓋

預設值、成功常開、失敗 / NACK / NotSent / timeout、脈衝與重複常開、不延長期限、到期單次解除、手動解除及失敗保留、從原開始時間改期限、停用 / 啟用、期限前後 Reload 恢復、離線待處理、兩筆驗證前禁止控制、重連後解除、NACK 與 uncertain 跨重啟不重播、手動 / timer 競態、已送出後停用、尚未送出時停用、過期 callback、Unload 取消交易、儲存失敗與 crash window、無效儲存、Config Entry 隔離、BOX 服務與同一 Device、原生 HA timer、實際 Store 的吞錯偵測、關機落盤與損壞 JSON。

## 資源與風險檢查

- 每 Entry 一個 manager、一個可取消 timer、最多一個 release task；任務明確關閉 eager start，避免同步完成時尚未登記 task 而產生重入。
- 沒有每秒輪詢、重試迴圈或新增 list / queue / history cache；只保存常數大小的設定與工作階段。
- timer generation 與開始 / 截止時間 token 避免過期 callback 或等待交易鎖的舊解除影響新工作階段。
- 停止先禁止新工作、取消 timer / task，再回收既有輪詢與 socket，等待交易結束後保存；Entity dispatcher listener 由 HA 移除。
- Store 維持 HA 原生格式與 executor I/O。常開命令前後各有小型磁碟寫入，前置 journal 保存失敗會拒絕送出，避免重啟後誤處理舊工作階段；需以實機確認磁碟延遲。
- Store 寫入確認使用兩個 HA 內部 write hooks；已檢查 2026.10.4 / 2026.11.1，HA 升級仍需重跑相容性測試。atomic write 不是硬體斷電耐久性保證。
- 常開 Switch 維持 assumed state；保存紀錄不是實體鎖回授。HA 停機 / 離線不能保證準時解除。交易未確認不自動重播。
- 無現場 EP-E300-485 / AR-725E 操作，未執行實機、現場時間準確度或硬體記憶體壓力驗收；README 提供完整現場流程。
