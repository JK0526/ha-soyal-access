"""Validate Lovelace deployment API and existing entity selection."""

import pytest
from homeassistant.setup import async_setup_component

from scripts.install_dashboard import card_config

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def test_card_resolves_renamed_entities_and_missing_fields():
    config = card_config(
        [
            {
                "unique_id": "entry:2:hold_open",
                "entity_id": "switch.renamed",
                "platform": "soyal_access",
                "config_entry_id": "entry",
            }
        ],
        "entry",
    )
    assert config["hold_open"] == "switch.renamed"
    assert "door_contact" not in config
    assert "events" not in config


async def test_dashboard_resource_and_config_api(hass, hass_ws_client):
    assert await async_setup_component(hass, "lovelace", {})
    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "lovelace/resources/create",
            "url": "/local/soyal-access/soyal-access-card.js?v=test",
            "res_type": "module",
        }
    )
    result = await client.receive_json()
    assert result["success"], result
    await client.send_json(
        {
            "id": 2,
            "type": "lovelace/dashboards/create",
            "url_path": "soyal-access",
            "title": "SOYAL",
            "show_in_sidebar": True,
            "require_admin": False,
        }
    )
    result = await client.receive_json()
    assert result["success"], result
    config = {
        "views": [
            {
                "title": "門禁",
                "panel": True,
                "cards": [
                    {"type": "custom:soyal-access-card", "events": "sensor.real_events"}
                ],
            }
        ]
    }
    await client.send_json(
        {
            "id": 3,
            "type": "lovelace/config/save",
            "url_path": "soyal-access",
            "config": config,
        }
    )
    result = await client.receive_json()
    assert result["success"], result
    await client.send_json(
        {"id": 4, "type": "lovelace/config", "url_path": "soyal-access"}
    )
    result = await client.receive_json()
    assert result["success"], result
    assert result["result"] == config
