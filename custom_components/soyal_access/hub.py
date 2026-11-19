"""Own the TCP connection for one Home Assistant config entry."""

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .const import DEFAULT_TIMEOUT, DOMAIN
from .protocol import ProtocolError
from .transport import Transport


class Hub:
    def __init__(self, hass, entry):
        self.signal = f"{DOMAIN}_{entry.entry_id}"
        self.entry_id = entry.entry_id
        self.node_device = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}:2")},
            via_device=(DOMAIN, entry.entry_id),
            name="SOYAL AR-725E — Node 02",
            manufacturer="SOYAL",
            model="AR-725E",
        )
        self._hass = hass
        self.confirmed = False
        self.busy = False
        self.result = "not_queried"
        self.last_success = None
        self.elapsed_ms = None
        self.transport = Transport(
            entry.data["host"],
            entry.data["port"],
            entry.data.get("timeout", DEFAULT_TIMEOUT),
            self.notify,
        )

    def notify(self):
        async_dispatcher_send(self._hass, self.signal)

    @property
    def can_query(self):
        return (
            self.confirmed
            and self.transport.connected
            and not self.transport.tainted
            and not self.busy
        )

    def confirm(self):
        if not self.transport.connected or self.transport.tainted or self.busy:
            raise HomeAssistantError("Gateway session is not eligible for confirmation")
        self.confirmed = True
        self.notify()

    async def query(self):
        if not self.can_query:
            raise HomeAssistantError("Node 02 query is disabled or busy")
        self.busy = True
        self.notify()
        try:
            elapsed = await self.transport.query_node02()
        except TimeoutError:
            self.result = "timeout"
        except ProtocolError:
            self.result = "invalid_response"
        except OSError:
            self.result = "communication_failed"
        else:
            self.result = "success"
            self.last_success = dt_util.utcnow().isoformat()
            self.elapsed_ms = round(elapsed * 1000, 3)
        finally:
            if self.transport.tainted or not self.transport.connected:
                self.confirmed = False
            self.busy = False
            self.notify()
