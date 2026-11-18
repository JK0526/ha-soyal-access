"""Validate gateway settings without opening a socket."""

import ipaddress
import re

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.selector import BooleanSelector

from .const import DEFAULTS, DOMAIN


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
            vol.Required("port", default=defaults["port"]): vol.All(
                int, vol.Range(min=1, max=65535)
            ),
            vol.Required("mode", default=defaults["mode"]): vol.In(["raw_tcp"]),
            vol.Required("stations", default=defaults["stations"]): str,
            vol.Required("timeout", default=defaults["timeout"]): vol.All(
                vol.Coerce(float), vol.Range(min=0.1, max=60)
            ),
            vol.Required(
                "reconnect_delay", default=defaults["reconnect_delay"]
            ): vol.All(vol.Coerce(float), vol.Range(min=0, max=60)),
            vol.Required(
                "enable_control", default=defaults.get("enable_control") is True
            ): vol.All(bool, BooleanSelector()),
        }
    )


class SoyalConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Create or reconfigure one gateway entry."""

    async def async_step_user(self, user_input=None):
        return await self._configure("user", DEFAULTS, user_input)

    async def async_step_reconfigure(self, user_input=None):
        entry = self._get_reconfigure_entry()
        return await self._configure("reconfigure", entry.data, user_input)

    async def _configure(self, step, defaults, user_input):
        errors = {}
        if user_input is not None:
            data = dict(user_input)
            try:
                data["host"] = normalized_host(data["host"])
            except vol.Invalid:
                errors["host"] = "invalid_host"
            stations = [part.strip() for part in data["stations"].split(",")]
            if not all(re.fullmatch(r"[0-9]{1,3}", part) for part in stations):
                errors["stations"] = "invalid_stations"
            else:
                nodes = [int(part) for part in stations]
                if len(set(nodes)) != len(nodes) or not all(
                    1 <= n <= 255 for n in nodes
                ):
                    errors["stations"] = "invalid_stations"
                data["stations"] = ",".join(map(str, nodes))
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
                return self.async_update_reload_and_abort(
                    entry, unique_id=key, title=key, data=data
                )
        return self.async_show_form(
            step_id=step,
            data_schema=schema(defaults | (user_input or {})),
            errors=errors,
        )
