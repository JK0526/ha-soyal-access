"""Own the TCP connection for one Home Assistant config entry."""

import asyncio
import json
from hashlib import sha256
from pathlib import Path

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .const import DEFAULT_TIMEOUT, DOMAIN
from .protocol import ProtocolError
from .transport import NotSentError, Transport


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
        endpoint = repr((entry.data["host"], entry.data["port"])).encode()
        key = sha256(endpoint).hexdigest()
        self._legacy_path = Path(hass.config.path(".storage", f"{DOMAIN}.{key}"))
        self.confirmed = False
        self.pulse_confirmed = False
        self.pulse_stopped = False
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
            self.confirmed and self.transport.connected and not self.transport.tainted
        )

    def confirm(self):
        if not self.transport.connected or self.transport.tainted or self.busy:
            raise HomeAssistantError("Gateway session is not eligible for confirmation")
        self.confirmed = True
        self.notify()

    async def query(self):
        if not self.can_query or self.busy:
            raise HomeAssistantError("Node 02 query is disabled or busy")
        self.busy = True
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
                self.confirmed = self.pulse_confirmed = False
            self.busy = False
            self.notify()

    @property
    def can_pulse(self):
        return (
            self.pulse_confirmed
            and not self.pulse_stopped
            and self.transport.connected
            and not self.transport.tainted
        )

    def _legacy_control_blocked(self):
        path = self._legacy_path
        try:
            if any(path.parent.glob(f"{path.name}.corrupt.*")):
                return True
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return False
            nodes = record["data"]["uncertain"]
            return (
                not isinstance(nodes, list)
                or any(type(node) is not int or not 1 <= node <= 255 for node in nodes)
                or 2 in nodes
            )
        except (OSError, ValueError, KeyError, TypeError):
            return True

    async def confirm_pulse(self):
        if await self._hass.async_add_executor_job(self._legacy_control_blocked):
            raise HomeAssistantError("Legacy uncertainty blocks Node 02 control")
        if (
            self.busy
            or self.pulse_stopped
            or self.transport.tainted
            or not self.transport.connected
        ):
            raise HomeAssistantError(
                "Gateway session is not eligible for pulse confirmation"
            )
        self.pulse_confirmed = True
        self.notify()

    async def pulse(self):
        if not self.can_pulse or self.busy:
            raise HomeAssistantError("Node 02 pulse was not sent: disabled or busy")
        self.busy = True
        try:
            code = await self.transport.pulse_node02()
            if code == 5:
                self.pulse_stopped = True
                raise HomeAssistantError("Node 02 rejected the pulse (NACK)")
        except NotSentError as err:
            raise HomeAssistantError("Node 02 pulse was not sent") from err
        except (TimeoutError, OSError, ProtocolError) as err:
            self.pulse_stopped = True
            raise HomeAssistantError(
                f"Node 02 pulse outcome unconfirmed ({type(err).__name__})"
            ) from err
        except asyncio.CancelledError:
            self.pulse_stopped = True
            raise
        finally:
            if (
                self.pulse_stopped
                or self.transport.tainted
                or not self.transport.connected
            ):
                self.pulse_confirmed = False
            self.busy = False
            self.notify()
