"""Bounded, entry-local hold-open protection; never infers physical lock state."""

import asyncio
import logging
from datetime import timedelta
from pathlib import Path

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DEFAULT_NODE_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)
_STATES = {
    "idle",
    "disabled",
    "active",
    "expired_pending",
    "releasing",
    "failed",
    "uncertain",
}


class ProtectionStore(Store):
    """Keep HA's storage format while detecting swallowed write failures.

    Store logs WriteError internally. These two write hooks are covered by
    compatibility tests so a failed journal is never accepted as durable.
    """

    async def async_load(self):
        existed = await self.hass.async_add_executor_job(Path(self.path).exists)
        data = await super().async_load()
        if existed and data is None:
            raise HomeAssistantError(
                "Existing protection storage could not be restored"
            )
        return data

    async def _async_write_data(self, data):
        await super()._async_write_data(data)
        self._write_confirmed = True

    async def async_save(self, data):
        self._write_confirmed = False
        await super().async_save(data)
        if self.hass.is_stopping:
            # Flush now rather than treating the deferred final-write as durable.
            await self._async_handle_write_data()
        if not self._write_confirmed:
            raise HomeAssistantError("Protection Store did not complete its write")


class HoldOpenTimeout:
    """One absolute deadline, one timer and at most one release task per entry.

    A write-ahead uncertain record precedes every hold/release command. Only a
    confirmed reply can replace it with a recoverable session. A crash between
    the command and the final save therefore cannot replay an ambiguous command.
    """

    def __init__(self, hass, hub):
        self._hass = hass
        self._hub = hub
        key = f"{DOMAIN}.{hub.entry_id}.hold_open_timeout"
        if hub.node_id != DEFAULT_NODE_ID:
            key = f"{key}.{hub.node_id}"
        self._store = ProtectionStore(hass, 1, key, atomic_writes=True)
        self.enabled = True
        self.minutes = 30
        self.started_at = None
        self.deadline = None
        self._phase = "idle"
        self.restore_safe = True
        self.signal = f"{hub.signal}_hold_open_timeout"
        self._lock = asyncio.Lock()
        self._timer = None
        self._generation = 0
        self._task = None
        self._closed = False
        self._prior = None
        self._recheck = False

    @property
    def can_change_node(self):
        """Only leave a station with no outstanding or unconfirmed hold."""
        return (
            self.restore_safe
            and self._phase == "idle"
            and self.started_at is None
            and self._task is None
        )

    @property
    def status(self):
        if not self.enabled and self._phase not in {"failed", "uncertain", "releasing"}:
            return "disabled"
        return self._phase

    @property
    def attributes(self):
        return {
            "timeout_enabled": self.enabled,
            "timeout_minutes": self.minutes,
            "timeout_active": self.enabled
            and self._phase in {"active", "expired_pending", "releasing"},
            "hold_open_started_at": self.started_at.isoformat()
            if self.started_at
            else None,
            "hold_open_deadline": self.deadline.isoformat() if self.deadline else None,
            "timeout_status": self.status,
            "timeout_restore_safe": self.restore_safe,
        }

    def _notify(self):
        async_dispatcher_send(self._hass, self.signal)

    async def async_load(self):
        """Restore only explicitly confirmed sessions, keeping Hub assumed state unknown."""
        try:
            data = await self._store.async_load()
            if data is None:
                return
            if not isinstance(data, dict):
                raise TypeError("Invalid protection record")
            enabled, minutes = data["timeout_enabled"], data["timeout_minutes"]
            phase = data["protection_state"]
            if (
                type(enabled) is not bool
                or type(minutes) is not int
                or not 1 <= minutes <= 1440
                or phase not in _STATES
            ):
                raise ValueError("Invalid protection settings/state")
            self.enabled, self.minutes = enabled, minutes
            start, deadline = data["hold_open_started_at"], data["hold_open_deadline"]
            if (start is None) != (deadline is None):
                raise ValueError("Incomplete protection deadline")
            if start is not None:
                start, deadline = (
                    dt_util.parse_datetime(start),
                    dt_util.parse_datetime(deadline),
                )
                if (
                    start is None
                    or deadline is None
                    or start.utcoffset() is None
                    or deadline.utcoffset() is None
                ):
                    raise ValueError("Protection timestamps must be timezone aware")
                start, deadline = dt_util.as_utc(start), dt_util.as_utc(deadline)
                if deadline != start + timedelta(minutes=minutes):
                    raise ValueError(
                        "Protection deadline does not match original start"
                    )
            if phase in {"active", "expired_pending"} and start is None:
                raise ValueError("Missing confirmed hold-open session")
            if phase == "idle" and start is not None:
                raise ValueError("Unexpected idle session")
            self.started_at, self.deadline = start, deadline
            self._phase = "uncertain" if phase in {"releasing", "uncertain"} else phase
            if self._phase == "disabled":
                self._phase = "active" if start else "idle"
            self._reschedule()
            if self._phase == "uncertain":
                _LOGGER.warning(
                    "SOYAL hold-open transaction interrupted/unconfirmed; no automatic replay"
                )
        except (HomeAssistantError, OSError, ValueError, TypeError, KeyError) as err:
            self._phase = "uncertain"
            self.restore_safe = False
            _LOGGER.warning(
                "SOYAL hold-open protection restore failed; automatic recovery unavailable: %s",
                err,
            )

    async def _save(self):
        data = {
            "timeout_enabled": self.enabled,
            "timeout_minutes": self.minutes,
            "hold_open_started_at": self.started_at.isoformat()
            if self.started_at
            else None,
            "hold_open_deadline": self.deadline.isoformat() if self.deadline else None,
            "protection_state": "uncertain" if self._prior is not None else self._phase,
        }
        try:
            await self._store.async_save(data)
        except (HomeAssistantError, OSError, ValueError, TypeError) as err:
            self.restore_safe = False
            _LOGGER.warning(
                "SOYAL hold-open protection persistence failed; restart recovery is not assured: %s",
                err,
            )
            return False
        self.restore_safe = True
        return True

    def _cancel_timer(self):
        self._generation += 1
        if self._timer is not None:
            self._timer()
            self._timer = None

    def _reschedule(self):
        self._cancel_timer()
        if (
            self._closed
            or self._prior is not None
            or self._phase in {"failed", "uncertain", "releasing"}
        ):
            return
        if self.started_at is None:
            self._phase = "idle"
            return
        self._phase = "active"
        if not self.enabled:
            return
        if self.deadline <= dt_util.utcnow():
            self._phase = "expired_pending"
            self.ready()
            return
        generation = self._generation

        @callback
        def expired(_now):
            if (
                generation != self._generation
                or self._closed
                or self._prior is not None
                or not self.enabled
                or self._phase != "active"
            ):
                return
            self._timer = None
            self._phase = "expired_pending"
            self._notify()
            self.ready()

        self._timer = async_track_point_in_utc_time(self._hass, expired, self.deadline)

    async def async_configure(self, *, enabled=None, minutes=None):
        if minutes is not None and (
            isinstance(minutes, bool)
            or not 1 <= minutes <= 1440
            or int(minutes) != minutes
        ):
            raise HomeAssistantError(
                "Hold-open timeout must be an integer from 1 to 1440 minutes"
            )
        async with self._lock:
            if self._closed:
                raise HomeAssistantError("Hold-open protection is stopping")
            if (enabled is None or enabled == self.enabled) and (
                minutes is None or minutes == self.minutes
            ):
                return
            if enabled is not None:
                self.enabled = enabled
            if minutes is not None:
                self.minutes = int(minutes)
            if self.started_at is not None:
                self.deadline = self.started_at + timedelta(minutes=self.minutes)
            self._recheck = self._task is not None
            self._reschedule()
            saved = await self._save()
            self._notify()
            if not saved:
                raise HomeAssistantError(
                    "Protection setting is active in memory but could not be saved"
                )

    @property
    def token(self):
        return (self.started_at, self.deadline, self._generation)

    def can_release(self, token):
        return (
            not self._closed
            and self.enabled
            and token == self.token
            and self.started_at is not None
            and self.deadline <= dt_util.utcnow()
            and self._phase in {"expired_pending", "releasing"}
        )

    async def async_prepare(self, *, automatic_token=None):
        """Called under Hub's transaction lock, before touching the transport."""
        async with self._lock:
            if self._closed or (
                automatic_token is not None and not self.can_release(automatic_token)
            ):
                return False
            self._prior = (self.started_at, self._phase)
            # Invalidate old callbacks without invalidating the caller's token.
            if self._timer is not None:
                self._timer()
                self._timer = None
            self._phase = "releasing" if automatic_token is not None else "uncertain"
            if not await self._save():
                self.started_at, self._phase = self._prior
                self._prior = None
                if automatic_token is not None:
                    self._phase = "failed"
                self._reschedule()
                self._notify()
                raise HomeAssistantError(
                    f"{self._hub.node_label} hold open was not sent: protection journal unavailable"
                )
            self._notify()
            return True

    async def async_complete(self, outcome, *, enabled, automatic=False):
        async with self._lock:
            if self._prior is None:
                return
            previous_start, previous_phase = self._prior
            self._prior = None
            if outcome == "success":
                if enabled:
                    self.started_at = previous_start or dt_util.utcnow()
                    # Repeating a confirmed hold never extends its original limit.
                    self._phase = (
                        previous_phase
                        if previous_start and previous_phase in {"failed", "uncertain"}
                        else "active"
                    )
                else:
                    self.started_at = None
                    self._phase = "idle"
            elif outcome == "uncertain":
                self._phase = "uncertain"
                _LOGGER.warning(
                    "SOYAL hold-open outcome unconfirmed; automatic release/replay suspended"
                )
            else:
                self.started_at, self._phase = previous_start, previous_phase
                if automatic and outcome == "nack":
                    self._phase = "failed"
                    _LOGGER.warning(
                        "SOYAL hold-open timeout release rejected (NACK); no automatic retry"
                    )
            self.deadline = (
                self.started_at + timedelta(minutes=self.minutes)
                if self.started_at
                else None
            )
            self._reschedule()
            await self._save()
            self._notify()

    def ready(self):
        """Wake only on expiry or Hub readiness changes; no retry/polling loop."""
        if (
            self._closed
            or not self.enabled
            or self._phase != "expired_pending"
            or self._prior is not None
            or not self._hub.can_pulse
            or self._hub.busy
            or (self._task is not None and not self._task.done())
        ):
            return
        token = self.token
        self._task = self._hass.async_create_background_task(
            self._release(token), "soyal_hold_open_timeout", eager_start=False
        )

    async def _release(self, token):
        try:
            await self._hub._control(
                0x83, "hold open timeout", hold_open=False, timeout_token=token
            )
        except HomeAssistantError as err:
            _LOGGER.warning("SOYAL hold-open timeout release: %s", err)
        finally:
            self._task = None
            # Do not retry an unavailable transaction in a tight task loop.
            if self._recheck:
                self._recheck = False
                self.ready()

    async def async_stop(self):
        self._closed = True
        self._cancel_timer()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def async_flush(self):
        async with self._lock:
            await self._save()
