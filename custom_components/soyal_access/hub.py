"""Own the TCP connection for one Home Assistant config entry."""

import logging
from hashlib import sha256
from pathlib import Path

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .const import DEFAULT_TIMEOUT, DOMAIN
from .protocol import ProtocolError
from .transport import NotSentError, Transport

_LOGGER = logging.getLogger(__name__)


class Hub:
    def __init__(self, hass, entry):
        self.signal = f"{DOMAIN}_{entry.entry_id}"
        self.entry_id = entry.entry_id
        endpoint = repr((entry.data["host"], entry.data["port"])).encode()
        key = sha256(endpoint).hexdigest()
        self._legacy_path = Path(hass.config.path(".storage", f"{DOMAIN}.{key}"))
        self.node_device = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}:2")},
            via_device=(DOMAIN, entry.entry_id),
            name="SOYAL AR-725E — Node 02",
            manufacturer="SOYAL",
            model="AR-725E",
        )
        self._hass = hass
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

    async def warn_legacy_uncertainty(self):
        exists = await self._hass.async_add_executor_job(self._legacy_path.exists)
        if exists:
            _LOGGER.warning(
                "Legacy SOYAL uncertainty record exists for this gateway. "
                "It is preserved and is not proof that old Gateway replies drained."
            )

    def notify(self):
        async_dispatcher_send(self._hass, self.signal)

    @property
    def can_query(self):
        return self.transport.connected and not self.transport.tainted

    async def query(self):
        if not self.can_query or self.busy:
            raise HomeAssistantError(
                "Node 02 query was not sent: Gateway unavailable or busy"
            )
        self.busy = True
        try:
            elapsed = await self.transport.query_node02()
        except NotSentError as err:
            raise HomeAssistantError("Node 02 query was not sent") from err
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
            self.busy = False
            self.notify()

    @property
    def can_pulse(self):
        return self.transport.connected and not self.transport.tainted

    async def pulse(self):
        if not self.can_pulse or self.busy:
            raise HomeAssistantError(
                "Node 02 pulse was not sent: Gateway unavailable or busy"
            )
        self.busy = True
        try:
            code = await self.transport.pulse_node02()
            if code == 5:
                raise HomeAssistantError("Node 02 rejected the pulse (NACK)")
        except NotSentError as err:
            raise HomeAssistantError("Node 02 pulse was not sent") from err
        except (TimeoutError, OSError, ProtocolError) as err:
            raise HomeAssistantError(
                f"Node 02 pulse outcome unconfirmed ({type(err).__name__})"
            ) from err
        finally:
            self.busy = False
            self.notify()
