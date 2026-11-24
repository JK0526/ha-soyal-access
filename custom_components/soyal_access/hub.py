"""Own the TCP connection for one Home Assistant config entry."""

import asyncio
import logging
import sqlite3
from hashlib import sha256
from pathlib import Path

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .const import DEFAULT_NODE_ID, DEFAULT_TIMEOUT, DOMAIN
from .fifo import parse_event
from .fifo_storage import FifoStorage
from .hold_open_timeout import HoldOpenTimeout
from .protocol import ProtocolError
from .transport import NotSentError, Transport

_LOGGER = logging.getLogger(__name__)
POLL_INTERVAL = 0.2
FIFO_INTERVAL = 2.0
RECONNECT_DELAYS = (1, 2, 4, 8, 15, 30)
RECOVERY_POLLS = 2


class Hub:
    def __init__(self, hass, entry, gateway_device_id):
        self.signal = f"{DOMAIN}_{entry.entry_id}"
        self.events_signal = f"{self.signal}_events_saved"
        self.entry_id = entry.entry_id
        self._entry = entry
        self.node_id = entry.data.get("node_id", DEFAULT_NODE_ID)
        self.node_uid = f"{entry.entry_id}:{self.node_id}"
        self.node_label = f"Node {self.node_id:02d}"
        endpoint = repr((entry.data["host"], entry.data["port"])).encode()
        key = sha256(endpoint).hexdigest()
        self._legacy_path = Path(hass.config.path(".storage", f"{DOMAIN}.{key}"))
        self._hass = hass
        self.node_device = DeviceInfo(
            identifiers={(DOMAIN, self.node_uid)},
            via_device_id=gateway_device_id,
            name=f"SOYAL AR-725E — {self.node_label}",
            manufacturer="SOYAL",
            model="AR-725E",
        )
        self.fifo_storage = FifoStorage(
            hass.config.path("soyal_access_fifo", f"{entry.entry_id}.sqlite3")
        )
        self.busy = False
        self._transaction = asyncio.Lock()
        self.poll_task = None
        self._fifo_task = None
        self.fifo_enabled = True
        self._fifo_delete_pending = False
        self._fifo_delete_waiting_for_poll = False
        self.stopping = False
        self.node_online = False
        self.unlock_output = None
        self._poll_failures = 0
        self._validation_polls = 0
        self._quiet_seconds = max(
            POLL_INTERVAL, entry.data.get("timeout", DEFAULT_TIMEOUT)
        )
        self.hold_open = None
        self.transport = Transport(
            entry.data["host"],
            entry.data["port"],
            entry.data.get("timeout", DEFAULT_TIMEOUT),
            self.notify,
            node_id=self.node_id,
        )

        self.hold_open_timeout = HoldOpenTimeout(hass, self)

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
        self.hold_open_timeout.ready()

    def start_polling(self):
        self.poll_task = asyncio.create_task(
            self._poll_loop(), name="soyal_access_poll"
        )

    async def stop(self):
        self.stopping = True
        await self.hold_open_timeout.async_stop()
        if self._fifo_task is not None:
            self._fifo_task.cancel()
            await asyncio.gather(self._fifo_task, return_exceptions=True)
            self._fifo_task = None
        if self.poll_task is not None:
            self.poll_task.cancel()
            await asyncio.gather(self.poll_task, return_exceptions=True)
            self.poll_task = None
        await self.transport.close()
        async with self._transaction:
            pass
        await self.hold_open_timeout.async_flush()

    async def _poll_loop(self):
        loop = asyncio.get_running_loop()
        due = loop.time() + self._quiet_seconds
        fifo_due = due + FIFO_INTERVAL
        backoff = 0
        while True:
            if not self.transport.connected:
                await asyncio.sleep(RECONNECT_DELAYS[backoff])
                backoff = min(backoff + 1, len(RECONNECT_DELAYS) - 1)
                if self.busy or self._transaction.locked():
                    continue
                await self.transport.start()
                due = loop.time() + self._quiet_seconds
                fifo_due = due + FIFO_INTERVAL
                if not self.transport.connected:
                    continue
                if self._fifo_delete_pending:
                    self._fifo_delete_waiting_for_poll = True

            fifo_delete_due = (
                loop.time()
                if self._fifo_delete_pending
                and not self._fifo_delete_waiting_for_poll
                and not self.busy
                and not self._transaction.locked()
                and self._can_exchange
                else float("inf")
            )
            fifo_wake = float("inf") if self._fifo_delete_pending else fifo_due
            await asyncio.sleep(
                max(0, min(due, fifo_wake, fifo_delete_due) - loop.time())
            )
            now = loop.time()
            if self._fifo_task is not None and self._fifo_task.done():
                await asyncio.gather(self._fifo_task, return_exceptions=True)
                self._fifo_task = None

            # 0x21 always wins when both schedules are due. Missed polls are
            # skipped by scheduling the next poll from its actual start time.
            if now >= due:
                due = now + POLL_INTERVAL
                if (
                    not self.busy
                    and self._can_exchange
                    and not self._transaction.locked()
                ):
                    async with self._transaction:
                        try:
                            frame = await self.transport.poll_node()
                        except (OSError, ProtocolError):
                            frame = None
                        if self._record_poll(frame):
                            backoff = 0
                            if self._fifo_delete_pending:
                                self._fifo_delete_waiting_for_poll = False
                if loop.time() >= due:
                    due = loop.time() + POLL_INTERVAL
                continue

            if not self._can_exchange:
                continue
            if self._fifo_delete_pending:
                if not self._fifo_delete_waiting_for_poll:
                    if self.busy or self._transaction.locked():
                        continue
                    await self._delete_fifo_event()
            elif now >= fifo_due:
                # Completion-based cadence: a busy control or read never
                # creates a backlog of FIFO checks.
                fifo_due = now + FIFO_INTERVAL
                if (
                    self.fifo_enabled
                    and self.can_query
                    and not self.busy
                    and not self._transaction.locked()
                    and self._fifo_task is None
                ):
                    self._fifo_task = asyncio.create_task(
                        self._read_fifo_event(), name="soyal_access_fifo"
                    )

    async def initialize_fifo_storage(self):
        """Create the new FIFO database without opening legacy databases."""
        try:
            await self._hass.async_add_executor_job(self.fifo_storage.initialize)
        except (OSError, sqlite3.Error) as err:
            self.fifo_enabled = False
            _LOGGER.error(
                "SOYAL FIFO storage unavailable, event sync disabled "
                "(door control and status are unaffected): %s",
                err,
            )

    async def _read_fifo_event(self):
        try:
            async with self._transaction:
                if self.busy or not self.can_query:
                    return
                frame = await self.transport.read_fifo_event()
            if frame.code == 4:
                return
            if frame.code == 5:
                _LOGGER.warning("SOYAL %s rejected FIFO read (NACK)", self.node_label)
                return

            event = parse_event(frame, self.node_id)
            received_at = dt_util.utcnow().isoformat()
            await self._hass.async_add_executor_job(
                self.fifo_storage.save_event, event, received_at
            )
            async_dispatcher_send(self._hass, self.events_signal)
            # Commit first, then wait for a verified 0x21 poll before 0x37.
            self._fifo_delete_pending = True
            self._fifo_delete_waiting_for_poll = True
        except (OSError, ProtocolError, sqlite3.Error, TimeoutError) as err:
            _LOGGER.warning("SOYAL %s FIFO read/save failed: %s", self.node_label, err)

    async def _delete_fifo_event(self):
        async with self._transaction:
            if (
                self.busy
                or self._fifo_delete_waiting_for_poll
                or not self._can_exchange
            ):
                return
            try:
                code = await self.transport.delete_fifo_event()
            except (OSError, ProtocolError, TimeoutError) as err:
                self._fifo_delete_pending = False
                self._fifo_delete_waiting_for_poll = False
                _LOGGER.warning("SOYAL %s FIFO delete failed: %s", self.node_label, err)
                return
        self._fifo_delete_pending = False
        self._fifo_delete_waiting_for_poll = False
        if code == 5:
            _LOGGER.warning("SOYAL %s rejected FIFO delete (NACK)", self.node_label)

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

    @property
    def can_pulse(self):
        # Updating entry data schedules a reload; the old Hub must reject control
        # immediately, including during the gap before platform unload.
        return self.can_query and self.node_id == self._entry.data.get(
            "node_id", DEFAULT_NODE_ID
        )

    async def pulse(self, *, user_id=None):
        await self._control(0x84, "pulse", user_id=user_id)

    async def set_hold_open(self, enabled: bool, *, user_id=None):
        await self._control(
            0x82 if enabled else 0x83, "hold open", hold_open=enabled, user_id=user_id
        )

    async def _control(
        self, action, label, *, hold_open=None, timeout_token=None, user_id=None
    ):
        if not self.can_pulse or self.busy:
            raise HomeAssistantError(
                f"{self.node_label} {label} was not sent: Gateway unavailable, validating, or busy"
            )
        self.busy = True
        sent = False
        prepared = False
        outcome = "not_sent"
        try:
            async with self._transaction:
                if not self.can_pulse:
                    raise NotSentError
                if hold_open is not None:
                    prepared = await self.hold_open_timeout.async_prepare(
                        automatic_token=timeout_token
                    )
                    if not prepared:
                        return
                    # Settings/stop may change while the write-ahead save awaits I/O.
                    if (
                        timeout_token is not None
                        and not self.hold_open_timeout.can_release(timeout_token)
                    ):
                        return
                    if not self.can_pulse or self._hass.is_stopping:
                        raise NotSentError
                sent = True
                code = await self.transport.control_node(action)
            if code == 5:
                outcome = "nack"
                raise HomeAssistantError(
                    f"{self.node_label} rejected the {label} (NACK)"
                )
            outcome = "success"
            succeeded_at = dt_util.utcnow()
            if hold_open is not None:
                self.hold_open = hold_open
        except NotSentError as err:
            outcome = "not_sent"
            raise HomeAssistantError(f"{self.node_label} {label} was not sent") from err
        except (TimeoutError, OSError, ProtocolError) as err:
            outcome = "uncertain"
            if hold_open is not None:
                self.hold_open = None
            raise HomeAssistantError(
                f"{self.node_label} {label} outcome unconfirmed ({type(err).__name__})"
            ) from err
        except asyncio.CancelledError:
            outcome = "uncertain" if sent else "not_sent"
            if sent and hold_open is not None:
                self.hold_open = None
            raise
        finally:
            try:
                if hold_open is not None:
                    # Also settles cancellation during the write-ahead save.
                    await self.hold_open_timeout.async_complete(
                        outcome, enabled=hold_open, automatic=timeout_token is not None
                    )
            finally:
                self.busy = False
                self.notify()

        if outcome == "success":
            event_type = (
                "ha_hold_open_timeout"
                if timeout_token is not None
                else {
                    0x84: "ha_pulse_open",
                    0x82: "ha_hold_open",
                    0x83: "ha_release_open",
                }[action]
            )
            await self._record_ha_operation(event_type, succeeded_at, user_id)

    async def _record_ha_operation(self, event_type, succeeded_at, user_id):
        """Keep history and auth I/O outside both the transaction lock and busy state."""
        ha_user_id = None
        ha_user_name = "系統" if event_type == "ha_hold_open_timeout" else None
        if user_id is not None and event_type != "ha_hold_open_timeout":
            try:
                user = await self._hass.auth.async_get_user(user_id)
                if user is not None:
                    ha_user_id, ha_user_name = user_id, user.name
            except (HomeAssistantError, OSError) as err:
                _LOGGER.warning("SOYAL HA operation user lookup failed: %s", err)
        try:
            await self._hass.async_add_executor_job(
                self.fifo_storage.save_ha_event,
                self.node_id,
                event_type,
                succeeded_at.isoformat(),
                dt_util.as_local(succeeded_at).isoformat(),
                ha_user_id,
                ha_user_name,
            )
        except (OSError, sqlite3.Error) as err:
            _LOGGER.warning(
                "SOYAL HA operation history save failed (control succeeded): %s", err
            )
            return
        async_dispatcher_send(self._hass, self.events_signal)
