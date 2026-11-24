"""Validate gateway settings without opening a socket."""

import ipaddress
import re

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import DEFAULT_NODE_ID, DEFAULT_PORT, DOMAIN, MAX_NODE_ID, MIN_NODE_ID


def normalized_host(value: str) -> str:
    host = value.strip().lower().rstrip(".")
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        if len(host) > 253 or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in host.split(".")
        ):
            raise vol.Invalid("invalid_host") from None
        return host


def schema(defaults):
    return vol.Schema(
        {
            vol.Required("host", default=defaults.get("host", "")): str,
            vol.Required("port", default=defaults.get("port", DEFAULT_PORT)): vol.All(
                int, vol.Range(min=1, max=65535)
            ),
            vol.Required(
                "node_id", default=defaults.get("node_id", DEFAULT_NODE_ID)
            ): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_NODE_ID, max=MAX_NODE_ID, step=1, mode=NumberSelectorMode.BOX
                )
            ),
        }
    )


class SoyalConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Create or reconfigure one gateway entry."""

    async def async_step_user(self, user_input=None):
        return await self._configure("user", {}, user_input)

    async def async_step_reconfigure(self, user_input=None):
        entry = self._get_reconfigure_entry()
        return await self._configure("reconfigure", entry.data, user_input)

    async def _configure(self, step, defaults, user_input):
        errors = {}
        if user_input is not None:
            data = {"node_id": defaults.get("node_id", DEFAULT_NODE_ID), **user_input}
            try:
                data["host"] = normalized_host(data["host"])
            except vol.Invalid:
                errors["host"] = "invalid_host"
            node_id = data["node_id"]
            if (
                type(node_id) not in (int, float)
                or not MIN_NODE_ID <= node_id <= MAX_NODE_ID
                or node_id % 1
            ):
                errors["node_id"] = "invalid_node_id"
            else:
                data["node_id"] = int(node_id)
            if not errors:
                key = f"[{data['host']}]:{data['port']}"
                await self.async_set_unique_id(key)
                if step == "user":
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(title=key, data=data)
                entry = self._get_reconfigure_entry()
                if any(
                    other.entry_id != entry.entry_id and other.unique_id == key
                    for other in self._async_current_entries()
                ):
                    return self.async_abort(reason="already_configured")
                if data["node_id"] != entry.data.get("node_id", DEFAULT_NODE_ID):
                    hub = getattr(entry, "runtime_data", None)
                    if (
                        entry.state is not config_entries.ConfigEntryState.LOADED
                        or hub is None
                        or hub.stopping
                        or hub.node_id != entry.data.get("node_id", DEFAULT_NODE_ID)
                    ):
                        return self.async_abort(reason="node_change_unavailable")
                    if (
                        hub.busy
                        or hub.hold_open is True
                        or not hub.hold_open_timeout.can_change_node
                    ):
                        return self.async_abort(reason="node_change_hold_open")
                updated = dict(entry.data) | data
                return self.async_update_reload_and_abort(
                    entry, unique_id=key, title=key, data=updated
                )
        return self.async_show_form(
            step_id=step,
            data_schema=schema(defaults | (user_input or {})),
            errors=errors,
        )
