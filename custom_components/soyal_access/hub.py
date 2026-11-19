"""Own the TCP connection for one Home Assistant config entry."""

from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import DEFAULT_TIMEOUT, DOMAIN
from .transport import Transport


class Hub:
    def __init__(self, hass, entry):
        self.signal = f"{DOMAIN}_{entry.entry_id}"
        self.transport = Transport(
            entry.data["host"],
            entry.data["port"],
            entry.data.get("timeout", DEFAULT_TIMEOUT),
            lambda: async_dispatcher_send(hass, self.signal),
        )
