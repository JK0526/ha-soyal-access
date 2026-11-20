"""Own the TCP connection for one Home Assistant config entry."""

import asyncio
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
POLL_INTERVAL = 0.2
RECONNECT_DELAYS = (1, 2, 4, 8, 15, 30)
RECOVERY_POLLS = 2


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
        self._transaction = asyncio.Lock()
        self.poll_task = None
        self.stopping = False
        self.node_online = False
        self.unlock_output = None
        self._poll_failures = 0
        self._validation_polls = 0
        self._quiet_seconds = max(
            POLL_INTERVAL, entry.data.get("timeout", DEFAULT_TIMEOUT)
        )
        self.hold_open = None
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
        if not self.transport.connected or self.transport.tainted:
            self.node_online = False
            self.unlock_output = None
            self._validation_polls = 0
        async_dispatcher_send(self._hass, self.signal)

    def start_polling(self):
        self.poll_task = asyncio.create_task(
            self._poll_loop(), name="soyal_access_poll"
        )

    async def stop(self):
        self.stopping = True
        if self.poll_task is not None:
            self.poll_task.cancel()
            await asyncio.gather(self.poll_task, return_exceptions=True)
            self.poll_task = None
        await self.transport.close()
        async with self._transaction:
            pass

    async def _poll_loop(self):
        loop = asyncio.get_running_loop()
        due = loop.time() + self._quiet_seconds
        backoff = 0
        while True:
            if not self.transport.connected:
                await asyncio.sleep(RECONNECT_DELAYS[backoff])
                backoff = min(backoff + 1, len(RECONNECT_DELAYS) - 1)
                if self.busy or self._transaction.locked():
                    continue
                await self.transport.start()
                due = loop.time() + self._quiet_seconds
                if not self.transport.connected:
                    continue

            await asyncio.sleep(max(0, due - loop.time()))
            started = loop.time()
            due = started + POLL_INTERVAL
            if self.busy or not self._can_exchange or self._transaction.locked():
                continue
            async with self._transaction:
                try:
                    frame = await self.transport.poll_node02()
                except (OSError, ProtocolError):
                    frame = None
                if self._record_poll(frame):
                    backoff = 0
            if loop.time() >= due:
                due = loop.time() + POLL_INTERVAL

    def _record_poll(self, frame):
        valid = frame is not None
        previous_output = self.unlock_output
        was_online = self.node_online
        was_ready = self.can_query
        if valid and self.transport.connected:
            self._poll_failures = 0
            self.node_online = True
            # Payload starts at raw offset 4 (source), so I/O offset 6 is index 2.
            self.unlock_output = bool(frame.payload[2] & 0x40)
            self._validation_polls = min(RECOVERY_POLLS, self._validation_polls + 1)
        else:
            if not was_ready:
                self._validation_polls = 0
            self._poll_failures = min(self._poll_failures + 1, 3)
            if self._poll_failures == 3 or not self.transport.connected:
                self.node_online = False
                self.unlock_output = None
        if (
            self.node_online != was_online
            or self.can_query != was_ready
            or self.unlock_output != previous_output
        ):
            self.notify()
        return valid and self.can_query

    @property
    def _can_exchange(self):
        return (
            not self.stopping
            and self.transport.connected
            and not self.transport.tainted
        )

    @property
    def can_query(self):
        return self._can_exchange and self._validation_polls == RECOVERY_POLLS

    async def query(self):
        if not self.can_query or self.busy:
            raise HomeAssistantError(
                "Node 02 query was not sent: Gateway unavailable, validating, or busy"
            )
        self.busy = True
        try:
            async with self._transaction:
                if not self.can_query:
                    raise NotSentError
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
        return self.can_query

    async def pulse(self):
        await self._control(0x84, "pulse")

    async def set_hold_open(self, enabled: bool):
        await self._control(0x82 if enabled else 0x83, "hold open", hold_open=enabled)

    async def _control(self, action, label, *, hold_open=None):
        if not self.can_pulse or self.busy:
            raise HomeAssistantError(
                f"Node 02 {label} was not sent: Gateway unavailable, validating, or busy"
            )
        self.busy = True
        sent = False
        try:
            async with self._transaction:
                if not self.can_pulse:
                    raise NotSentError
                sent = True
                code = await self.transport.control_node02(action)
            if code == 5:
                raise HomeAssistantError(f"Node 02 rejected the {label} (NACK)")
            if hold_open is not None:
                self.hold_open = hold_open
        except NotSentError as err:
            raise HomeAssistantError(f"Node 02 {label} was not sent") from err
        except (TimeoutError, OSError, ProtocolError) as err:
            if hold_open is not None:
                self.hold_open = None
            raise HomeAssistantError(
                f"Node 02 {label} outcome unconfirmed ({type(err).__name__})"
            ) from err
        except asyncio.CancelledError:
            if sent and hold_open is not None:
                self.hold_open = None
            raise
        finally:
            self.busy = False
            self.notify()
