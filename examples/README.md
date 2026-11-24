# Dashboard 與門禁歷史查詢

範本適用於 SOYAL Access Control **v0.8.0** 與 **Home Assistant 2026.8.0 或更新版本**。使用 HA 原生 Sections、Heading、Tile、Button 與 Markdown，桌面最多三欄，手機依畫面寬度排列。

## Step 1 · 完成 SOYAL 整合安裝

依 [專案說明](../README.md) 安裝並連上控制器。以下以 Node 02 為例。

## Step 2 · 確認門禁 Entity ID

到「設定 → 裝置與服務 → SOYAL Access Control → 控制器裝置」或「開發者工具 → 狀態」查找以下實體。將 [Dashboard 範本](dashboard.yaml) 中所有對應的範例 ID 一併替換，包括 Markdown 模板中的 ID。

| 功能 | 領域 | v0.8.0 識別後綴 |
| --- | --- | --- |
| 主門解鎖輸出 | binary_sensor | unlock_output |
| 單次開門 | button | pulse_open |
| 常開 | switch | hold_open |
| 常開超時保護 | switch | hold_open_timeout_protection |
| 常開時間上限 | number | hold_open_timeout |
| 最近門禁事件 | sensor | access_event_history |

識別後綴屬於 Unique ID。實際 Entity ID 會受語言、裝置名稱及既有命名影響。歷史卡讀取 Sensor 的 `events` 屬性。

## Step 3 · 建立四個 Helper

**UI 與 YAML 二選一。** 如用 UI，進入「設定 → 裝置與服務 → 輔助元素 → 建立輔助元素」，依下表設定。建立後可在實體設定中將 ID 改成表中所列，或同步修改所有範本引用。

| 類型 | 最終 Entity ID | 設定 |
| --- | --- | --- |
| 日期和時間 | `input_datetime.input_datetime_soyal_query_date` | 只選日期 |
| 數字 | `input_number.input_number_soyal_query_user` | 0–65534、步進 1、輸入框 |
| 按鈕 | `input_button.input_button_soyal_query` | SOYAL 查詢紀錄 |
| 文字 | `input_text.input_text_soyal_query_result` | 最大長度 255 |

如用 YAML，將 [helpers.yaml](helpers.yaml) 的四個區塊合併至 `configuration.yaml`，已有同名頂層區塊時合併子項目，儲存後重新啟動 HA。人員編號是 FIFO 的 `user_address`，0–65534 是範本查詢範圍，實際可用編號以設備韌體與容量模式為準。 [SOYAL AR-725E 規格](https://www.soyal.com/product.php?act=view&id=1)

## Step 4 · 確認 SQLite 資料庫路徑

整合成功載入後，以具 HA 設定目錄存取權的檔案工具查找 `/config/soyal_access_fifo/`。也可在 HA 終端執行 `ls /config/soyal_access_fifo/*.sqlite3`。檔名去掉 `.sqlite3` 即為 Config Entry ID。若有多筆整合，可用開發者工具的模板 `{{ config_entry_id('REPLACE_WITH_HISTORY_SENSOR_ENTITY_ID') }}` 對照指定歷史 Sensor。將自動化中的 `REPLACE_WITH_ENTRY_ID` 換成對應檔名，並確認檔案已存在。SQLite Web 可選用來查看資料。

## Step 5 · 建立歷史查詢 Automation

先從「設定 → 裝置與服務 → 新增整合」加入 HA 原生 **SQL**，建立至少一個 SQL Sensor 以註冊 `sql.query`。可用上一步的 Database URL，Query 填 `SELECT COUNT(*) AS event_count FROM access_events`、Column 填 `event_count`。SQL Sensor 只用於載入整合，可關閉其定期輪詢並保留整合。到「開發者工具 → 動作」確認 `sql.query` 可選用。該動作自 Home Assistant 2025.11 起提供，本套範本以 SOYAL 整合要求的 2026.8.0 為下限。 [SQL Query 文件](https://www.home-assistant.io/actions/sql.query/) · [2025.11 更新紀錄](https://www.home-assistant.io/changelogs/core-2025.11/)

建立空白自動化，從選單選「以 YAML 編輯」，貼入 [automation.yaml](automation.yaml)。更新資料庫路徑與 `node_id`，儲存並啟用。查詢依控制器本地日期、站號、人員編號篩選 SOYAL 原生事件，最多顯示 5 筆。結果前綴記錄站號、日期、人員及查詢狀態，後方以 `HH:mm:ss|event_type` 表示每列。

## Step 6 · 匯入 Dashboard YAML

建立空白 Dashboard，開啟編輯模式與「原始設定編輯器」，貼上 [dashboard.yaml](dashboard.yaml)。加入既有 Dashboard 時，只合併 `views` 內的新檢視。依 Step 2 替換實體 ID。全部卡片使用 HA 內建功能。

## Step 7 · 修改站號及人員名稱

站號不同時，更新 Dashboard 所有門禁實體 ID、查詢 Markdown 卡中的 `node_id`、以及 Automation 的 `node_id`。如果改用另一筆整合，亦更新資料庫路徑。可在最近事件卡中改寫 `user_map`，以整數地址對應顯示名稱。HA 事件使用紀錄當下的 `ha_user_name`。

## Step 8 · 驗證查詢及門禁控制

先選有刷卡紀錄的日期與人員，按「查詢紀錄」並核對時間與筆數。改變條件應提示重新查詢。改查無資料的日期，應顯示 0 筆。接著在可監看門鎖時，確認單次開門與解除常開可直接操作，啟用常開與切換超時保護需確認。調整超時上限前會先確認開啟設定視窗。上述確認適用於本 Dashboard 的操作入口。
