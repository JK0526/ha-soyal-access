"""Deploy the SOYAL card and a storage-mode Lovelace dashboard."""

import argparse
import asyncio
import hashlib
import json
import os
import shutil
from pathlib import Path

import aiohttp


def card_config(entities, entry_id):
    """Resolve renamed entities by integration Unique ID."""
    suffixes = {
        "gateway_online": "gateway:tcp_connected",
        "node_online": "2:node_online",
        "hold_open": "2:hold_open",
        "unlock_output": "2:unlock_output",
        "events": "2:access_event_history",
    }
    by_id = {
        item["unique_id"]: item["entity_id"]
        for item in entities
        if item["platform"] == "soyal_access"
        and item["config_entry_id"] == entry_id
        and not item.get("disabled_by")
    }
    config = {"type": "custom:soyal-access-card", "title": "大門"}
    config.update(
        (key, by_id[f"{entry_id}:{suffix}"])
        for key, suffix in suffixes.items()
        if f"{entry_id}:{suffix}" in by_id
    )
    return config


async def install(args):
    source = Path(__file__).resolve().parents[1] / "dashboard/soyal-access-card.js"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:12]
    target = args.config_dir / "www/soyal-access/soyal-access-card.js"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    resource_url = f"/local/soyal-access/soyal-access-card.js?v={digest}"
    ws_url = (
        args.url.rstrip("/")
        .replace("https://", "wss://", 1)
        .replace("http://", "ws://", 1)
        + "/api/websocket"
    )
    token = os.environ.get("HA_TOKEN")
    if not token:
        raise RuntimeError("請設定 HA_TOKEN 為管理員長期存取權杖")
    async with aiohttp.ClientSession() as session, session.ws_connect(ws_url) as ws:
        await ws.receive_json()
        await ws.send_json({"type": "auth", "access_token": token})
        if (await ws.receive_json())["type"] != "auth_ok":
            raise RuntimeError("Home Assistant 認證失敗")
        command_id = 0

        async def command(kind, **data):
            nonlocal command_id
            command_id += 1
            await ws.send_json({"id": command_id, "type": kind, **data})
            while True:
                result = await ws.receive_json()
                if result.get("id") == command_id:
                    if not result.get("success"):
                        raise RuntimeError(result.get("error"))
                    return result["result"]

        entities = await command("config/entity_registry/list")
        entries = sorted(
            {e["config_entry_id"] for e in entities if e["platform"] == "soyal_access"}
        )
        entry_id = args.entry_id or (entries[0] if len(entries) == 1 else None)
        if entry_id not in entries:
            raise RuntimeError(f"請用 --entry-id 指定其中一個 Config Entry {entries}")
        if not args.skip_resource:
            resources = await command("lovelace/resources")
            resource = next(
                (
                    r
                    for r in resources
                    if r["url"].split("?")[0] == resource_url.split("?")[0]
                ),
                None,
            )
            if resource:
                await command(
                    "lovelace/resources/update",
                    resource_id=resource["id"],
                    url=resource_url,
                    res_type="module",
                )
            else:
                await command(
                    "lovelace/resources/create", url=resource_url, res_type="module"
                )
        dashboards = await command("lovelace/dashboards/list")
        existing = next((d for d in dashboards if d["url_path"] == args.path), None)
        if existing and not args.replace:
            raise RuntimeError("Dashboard 已存在，更新請明確指定 --replace")
        if not existing:
            await command(
                "lovelace/dashboards/create",
                url_path=args.path,
                title="門禁（SOYAL）",
                icon="mdi:door",
                show_in_sidebar=True,
                require_admin=False,
            )
        card = card_config(entities, entry_id)
        if args.door_contact:
            card["door_contact"] = args.door_contact
        await command(
            "lovelace/config/save",
            url_path=args.path,
            config={
                "title": "門禁（SOYAL）",
                "views": [
                    {
                        "title": "門禁",
                        "path": "access",
                        "panel": True,
                        "cards": [card],
                    }
                ],
            },
        )
        print(
            json.dumps(
                {
                    "dashboard": f"{args.url.rstrip('/')}/{args.path}/access",
                    "resource": resource_url,
                    "card": card,
                },
                ensure_ascii=False,
            )
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--config-dir", required=True, type=Path)
    parser.add_argument("--entry-id")
    parser.add_argument("--door-contact")
    parser.add_argument("--path", default="soyal-access")
    parser.add_argument("--replace", action="store_true")
    parser.add_argument(
        "--skip-resource",
        action="store_true",
        help="資源由既有 YAML 管理時略過資源註冊",
    )
    asyncio.run(install(parser.parse_args()))


if __name__ == "__main__":
    main()
