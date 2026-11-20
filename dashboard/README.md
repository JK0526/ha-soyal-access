# SOYAL Lovelace Dashboard

無外部 JS 依賴的 Web Component，直接使用 HA entities 與 events attribute。HA 每次傳入新的 hass 狀態即更新畫面。

## 安裝

先安裝本分支整合並重啟 HA，確認最近門禁事件 Sensor 存在。在能存取 HA config 目錄與 URL 的電腦使用 Python 3.13 執行。需要 aiohttp 套件，HA Python 環境已包含。

1. config 目錄若尚無 www，先建立並重新啟動 HA，讓 /local 路徑可用。
2. 在 HA 個人資料建立管理員長期存取權杖，以環境變數 HA_TOKEN 提供。避免將權杖寫入命令列參數或 Git。
3. 在 repository 執行下列命令（以實際路徑與 URL 替換）。

    python3 scripts/install_dashboard.py --url http://homeassistant.local:8123 --config-dir /config

工具會複製 JS 至 www/soyal-access，透過 Unique ID 找到重新命名過的實體，註冊 module resource，建立側欄「門禁（SOYAL）」。
多個 Config Entry 時，依工具列出的 ID 使用 --entry-id 選擇。
選填 --door-contact binary_sensor.your_contact，約定 on 表示開啟、off 表示關閉。SOYAL 整合目前無已確認的門磁來源，未指定時顯示未知。

自動安裝使用 HA 預設 storage resource 管理。resource_mode: yaml 的環境請將複製的 JS 以 module 加入既有 resources YAML 並重新載入資源。API 拒絕時工具會回報錯誤，可依既有 YAML 資源配置後加 --skip-resource 重跑。工具使用 WebSocket API 管理 Dashboard。

## 更新

    python3 scripts/install_dashboard.py --url http://homeassistant.local:8123 --config-dir /config --replace

--replace 會重新產生指定 Dashboard，若已自行修改請先下載備份設定。
--path soyal-second 可使用其他 Dashboard 路徑。
JS URL 使用內容 SHA256 作為快取版本。更新後重新整理瀏覽器或 Companion App，載入新 module。
Card 提供視覺設定表單，可調整名稱及 Entity ID。
在 HA 個人資料選擇深色模式，Card 會使用 HA 主題變數。桌面雙欄，手機單欄。

## 資料語意

- 門鎖狀態來自解鎖輸出，常開標示同時要求輸出 on 與常開命令 on，實體鎖舌位置需現場確認。
- 常開按鈕使用既有 switch.turn_on／turn_off，錯誤顯示在 Card。
- Relay 區顯示已確認的解鎖輸出。
- events 依 id 降冪最多 100 筆，最後一筆使用相同排序。
- 控制器本地時間直接呈現並標註，缺少時以瀏覽器時區顯示接收時間。
- 人員顯示 user_address，現有資料無姓名或卡號。事件標示為卡片／出門／密碼事件，通行結果標示未確認。
- 缺少實體、不可用及無事件分別呈現未知或空狀態。

官方資源載入及快取更新文件
https://contributors.home-assistant.io/frontend/custom-ui/resources/

## 驗證

pytest 包含 HA Lovelace WebSocket 資源與 Dashboard API 測試。
Chromium Card 測試透過注入 HA 狀態檢查更新、操作、空資料、缺少實體與手機排版。
瀏覽器測試需 pip install playwright，並執行 python3 -m playwright install chromium。
實機仍需核對實體、門磁極性、常開動作及 FIFO 新事件顯示。
