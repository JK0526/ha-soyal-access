"""Browser checks using injected HA state and captured service calls."""

from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.usefixtures("socket_enabled")


def test_card_browser():
    source = Path("dashboard/soyal-access-card.js").read_text()
    source = source.replace("export function", "function").replace(
        "export class", "class"
    )
    with playwright.sync_playwright() as api:
        browser = api.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.set_content(
            "<body style='margin:0;background:#10151c;color:#eef1f5'></body>"
        )
        page.add_script_tag(content=source)
        page.evaluate("""() => {
          window.calls = [];
          window.card = document.createElement("soyal-access-card");
          card.setConfig({hold_open:"switch.door", unlock_output:"binary_sensor.output",
            gateway_online:"binary_sensor.tcp", node_online:"binary_sensor.node",
            events:"sensor.events"});
          document.body.append(card);
          window.ha = {states:{
            "switch.door":{state:"off"}, "binary_sensor.output":{state:"off"},
            "binary_sensor.tcp":{state:"on"}, "binary_sensor.node":{state:"on"},
            "sensor.events":{state:"0",attributes:{events:[]}}
          }, callService: async (...args) => calls.push(args)};
          card.hass = ha;
        }""")
        assert "連線正常" in page.locator("ha-card").inner_text()
        assert "尚無可顯示的紀錄" in page.locator("ha-card").inner_text()
        page.get_by_role("button", name="啟用常開", exact=True).click()
        assert page.evaluate("calls") == [
            ["switch", "turn_on", {"entity_id": "switch.door"}]
        ]
        page.evaluate("""() => {
          ha.states["sensor.events"] = {state:"2", attributes:{events:[
            {id:1,received_at:"2026-11-20T00:00:00Z",user_address:100,event_type:"card_access",port:2},
            {id:2,event_time_local:"2026-11-20T12:00:00",user_address:"<script>bad</script>",event_type:"unverified",port:2}
          ]}};
          ha.states["binary_sensor.output"]={state:"on"};
          ha.states["switch.door"]={state:"on"};
          card.hass = ha;
        }""")
        assert "<script>bad</script>" in page.locator(".latest").inner_text()
        assert page.locator(".latest script").count() == 0
        assert "通行結果未確認" in page.locator(".latest").inner_text()
        assert "常開（解鎖輸出中）" in page.locator(".status").inner_text()
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.body.scrollWidth") <= 390
        page.evaluate("ha.states = {}; card.hass = ha")
        assert page.get_by_role("button", name="啟用常開", exact=True).is_disabled()
        assert "未提供／未知" in page.locator("ha-card").inner_text()
        browser.close()
