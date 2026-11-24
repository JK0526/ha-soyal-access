"""Deterministic clock, transaction races, persistence and native entity tests."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.soyal_access.const import DOMAIN
from custom_components.soyal_access.hub import Hub
from custom_components.soyal_access.transport import NotSentError

_REAL_STORE_WRITE = Store._async_write_data
_REAL_STORE_LOAD = Store._async_load


@pytest.fixture
def real_store_io(hass_storage):
    """Remove the HA test plugin's in-memory storage hooks for disk failure tests."""
    with (
        patch.object(Store, "_async_write_data", _REAL_STORE_WRITE),
        patch.object(Store, "_async_load", _REAL_STORE_LOAD),
    ):
        yield


class MemoryStore:
    def __init__(self):
        self.data = None
        self.fail = False
        self.writes = []

    async def async_load(self):
        return deepcopy(self.data)

    async def async_save(self, data):
        if self.fail:
            raise OSError("disk unavailable")
        self.data = deepcopy(data)
        self.writes.append(deepcopy(data))


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 24, tzinfo=UTC)
        self.handles = []

    def track(self, _hass, callback, deadline):
        handle = SimpleNamespace(callback=callback, deadline=deadline, cancelled=False)
        self.handles.append(handle)

        def cancel():
            handle.cancelled = True

        return cancel

    def advance(self, *, minutes=0, seconds=0):
        self.now += timedelta(minutes=minutes, seconds=seconds)
        for handle in list(self.handles):
            if not handle.cancelled and handle.deadline <= self.now:
                handle.cancelled = True
                handle.callback(self.now)

    @property
    def active(self):
        return [handle for handle in self.handles if not handle.cancelled]


@pytest.fixture
async def rig(hass, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(
        "custom_components.soyal_access.hold_open_timeout.dt_util",
        SimpleNamespace(
            utcnow=lambda: clock.now,
            parse_datetime=dt_util.parse_datetime,
            as_utc=dt_util.as_utc,
        ),
    )
    monkeypatch.setattr(
        "custom_components.soyal_access.hold_open_timeout.async_track_point_in_utc_time",
        clock.track,
    )
    store = MemoryStore()
    hubs = []

    async def create(*, online=True, storage=store):
        entry = MockConfigEntry(domain=DOMAIN, data={"host": "127.0.0.1", "port": 502})
        hub = Hub(hass, entry, "gateway-device-id")
        hub.transport = SimpleNamespace(
            connected=online,
            tainted=False,
            control_node02=AsyncMock(return_value=4),
            close=AsyncMock(),
        )
        hub._validation_polls = 2 if online else 0
        hub.hold_open_timeout._store = storage
        await hub.hold_open_timeout.async_load()
        hubs.append(hub)
        return hub

    value = SimpleNamespace(clock=clock, store=store, create=create)
    value.hub = await create()
    value.manager = value.hub.hold_open_timeout
    yield value
    for hub in hubs:
        await hub.stop()


async def settle(manager):
    for _ in range(10):
        task = manager._task
        if task is None:
            return
        await task
    raise AssertionError("Unbounded timeout tasks")


async def test_defaults_success_pulse_repeat_manual_release(rig):
    hub, manager = rig.hub, rig.manager
    assert manager.enabled and manager.minutes == 30
    assert manager.status == "idle" and not rig.clock.active
    await hub.set_hold_open(True)
    original = manager.deadline
    assert manager.status == "active"
    assert original == rig.clock.now + timedelta(minutes=30)
    assert rig.store.writes[-2]["protection_state"] == "uncertain"
    assert rig.store.data["protection_state"] == "active"
    rig.clock.advance(minutes=5)
    await hub.pulse()
    await hub.set_hold_open(True)
    assert manager.deadline == original and len(rig.clock.active) == 1
    await hub.set_hold_open(False)
    assert manager.status == "idle" and manager.started_at is None
    assert not rig.clock.active
    rig.clock.advance(minutes=60)
    await settle(manager)
    assert hub.transport.control_node02.call_count == 4


@pytest.mark.parametrize("failure", [5, NotSentError(), TimeoutError()])
async def test_hold_failure_never_starts_session(rig, failure):
    if isinstance(failure, int):
        rig.hub.transport.control_node02.return_value = failure
    else:
        rig.hub.transport.control_node02.side_effect = failure
    with pytest.raises(HomeAssistantError):
        await rig.hub.set_hold_open(True)
    assert rig.manager.started_at is None
    assert not rig.clock.active
    rig.clock.advance(minutes=100)
    rig.hub.notify()
    await settle(rig.manager)
    assert rig.hub.transport.control_node02.call_count == 1


async def test_timeout_releases_exactly_once(rig):
    await rig.hub.set_hold_open(True)
    rig.clock.advance(minutes=30)
    await settle(rig.manager)
    assert rig.hub.transport.control_node02.call_args_list[-1].args == (0x83,)
    assert rig.manager.status == "idle" and rig.hub.hold_open is False
    for _ in range(10):
        rig.hub.notify()
    rig.clock.advance(minutes=60)
    assert rig.hub.transport.control_node02.call_count == 2
    assert not rig.clock.active


async def test_minutes_use_original_start_and_past_deadline(rig):
    await rig.hub.set_hold_open(True)
    start = rig.manager.started_at
    rig.clock.advance(minutes=10)
    await rig.manager.async_configure(minutes=20)
    assert rig.manager.deadline == start + timedelta(minutes=20)
    assert len(rig.clock.active) == 1
    await rig.manager.async_configure(minutes=5)
    await settle(rig.manager)
    assert rig.manager.status == "idle"
    assert rig.hub.transport.control_node02.call_count == 2


async def test_disable_reenable_and_unknown_output(rig):
    await rig.hub.set_hold_open(True)
    deadline = rig.manager.deadline
    await rig.manager.async_configure(enabled=False)
    assert rig.manager.status == "disabled" and not rig.clock.active
    rig.clock.advance(minutes=31)
    assert rig.hub.transport.control_node02.call_count == 1
    await rig.manager.async_configure(enabled=True)
    assert rig.manager.deadline == deadline
    await settle(rig.manager)
    assert rig.manager.status == "idle"
    rig.hub.unlock_output = True
    await rig.manager.async_configure(enabled=False)
    await rig.manager.async_configure(enabled=True)
    assert rig.manager.started_at is None
    assert rig.hub.transport.control_node02.call_count == 2


@pytest.mark.parametrize("elapsed", [10, 40])
async def test_reload_restores_remaining_deadline_and_waits_validation(rig, elapsed):
    await rig.hub.set_hold_open(True)
    deadline = rig.manager.deadline
    await rig.hub.stop()
    assert not rig.clock.active
    rig.clock.advance(minutes=elapsed)
    hub = await rig.create(online=False)
    manager = hub.hold_open_timeout
    assert hub.hold_open is None
    assert manager.deadline == deadline
    assert manager.minutes == 30
    assert manager.status == ("active" if elapsed == 10 else "expired_pending")
    rig.clock.advance(minutes=max(0, 30 - elapsed))
    assert manager.status == "expired_pending"
    hub.transport.connected = True
    hub._validation_polls = 1
    hub.notify()
    await settle(manager)
    hub.transport.control_node02.assert_not_called()
    hub._validation_polls = 2
    hub.notify()
    await settle(manager)
    hub.transport.control_node02.assert_awaited_once_with(0x83)
    assert manager.status == "idle"


async def test_offline_pending_then_reconnect(rig):
    await rig.hub.set_hold_open(True)
    rig.hub.transport.connected = False
    rig.hub.notify()
    rig.clock.advance(minutes=31)
    assert rig.manager.status == "expired_pending"
    assert rig.manager._task is None
    assert rig.hub.transport.control_node02.call_count == 1
    rig.hub.transport.connected = True
    rig.hub._validation_polls = 2
    rig.hub.notify()
    await settle(rig.manager)
    assert rig.manager.status == "idle"


@pytest.mark.parametrize("error,status", [(5, "failed"), (TimeoutError(), "uncertain")])
async def test_release_failure_never_automatically_retries_even_after_restart(
    rig, error, status
):
    await rig.hub.set_hold_open(True)
    if isinstance(error, int):
        rig.hub.transport.control_node02.return_value = error
    else:
        rig.hub.transport.control_node02.side_effect = error
    rig.clock.advance(minutes=30)
    await settle(rig.manager)
    assert rig.manager.status == status
    assert rig.manager.started_at is not None
    for _ in range(20):
        rig.hub.notify()
    await settle(rig.manager)
    assert rig.hub.transport.control_node02.call_count == 2
    await rig.hub.stop()
    hub = await rig.create()
    for _ in range(20):
        hub.notify()
    await settle(hub.hold_open_timeout)
    assert hub.hold_open_timeout.status == status
    hub.transport.control_node02.assert_not_called()
    await hub.hold_open_timeout.async_configure(enabled=False)
    await hub.hold_open_timeout.async_configure(enabled=True)
    hub.transport.control_node02.assert_not_called()


async def test_unsent_release_is_pending_without_busy_retry_loop(rig):
    await rig.hub.set_hold_open(True)
    rig.hub.transport.control_node02.side_effect = NotSentError()
    rig.clock.advance(minutes=30)
    await settle(rig.manager)
    assert rig.manager.status == "expired_pending"
    assert rig.hub.transport.control_node02.call_count == 2
    # A later successful readiness transition can safely retry an unsent command.
    rig.hub.transport.control_node02.side_effect = None
    rig.hub.notify()
    await settle(rig.manager)
    assert rig.manager.status == "idle"


async def test_manual_release_nack_retains_timer(rig):
    await rig.hub.set_hold_open(True)
    deadline = rig.manager.deadline
    rig.hub.transport.control_node02.return_value = 5
    with pytest.raises(HomeAssistantError, match="NACK"):
        await rig.hub.set_hold_open(False)
    assert rig.manager.deadline == deadline
    assert rig.manager.status == "active" and len(rig.clock.active) == 1


async def test_manual_release_wins_race_and_stale_callback_cannot_release_new_session(
    rig,
):
    await rig.hub.set_hold_open(True)
    stale = rig.clock.active[0].callback
    await rig.hub._transaction.acquire()
    task = asyncio.create_task(rig.hub.set_hold_open(False))
    await asyncio.sleep(0)
    assert rig.hub.busy
    rig.clock.advance(minutes=30)
    assert rig.manager._task is None
    rig.hub._transaction.release()
    await task
    await rig.hub.set_hold_open(True)
    deadline = rig.manager.deadline
    stale(rig.clock.now)
    await settle(rig.manager)
    assert rig.manager.deadline == deadline and rig.manager.status == "active"
    assert rig.hub.transport.control_node02.call_count == 3


async def test_disable_queued_timeout_cancels_unsent_release(rig):
    await rig.hub.set_hold_open(True)
    await rig.hub._transaction.acquire()
    rig.clock.advance(minutes=30)
    await asyncio.sleep(0)
    assert rig.hub.busy
    await rig.manager.async_configure(enabled=False)
    rig.hub._transaction.release()
    await settle(rig.manager)
    assert rig.manager.status == "disabled"
    assert rig.hub.transport.control_node02.call_count == 1


async def test_disable_sent_release_preserves_actual_outcome(rig):
    await rig.hub.set_hold_open(True)
    sent, finish = asyncio.Event(), asyncio.Event()

    async def exchange(_action):
        sent.set()
        await finish.wait()
        return 4

    rig.hub.transport.control_node02.side_effect = exchange
    rig.clock.advance(minutes=30)
    await sent.wait()
    assert rig.manager.status == "releasing"
    await rig.manager.async_configure(enabled=False)
    assert rig.store.data["protection_state"] == "uncertain"
    finish.set()
    await settle(rig.manager)
    assert rig.manager.started_at is None
    assert rig.hub.hold_open is False
    assert rig.manager.status == "disabled"


async def test_unload_cancels_inflight_release_and_journals_uncertainty(rig):
    await rig.hub.set_hold_open(True)
    sent = asyncio.Event()

    async def exchange(_action):
        sent.set()
        await asyncio.Event().wait()

    rig.hub.transport.control_node02.side_effect = exchange
    rig.clock.advance(minutes=30)
    await sent.wait()
    task = rig.manager._task
    await rig.hub.stop()
    assert task.done() and rig.manager._task is None
    assert not rig.clock.active
    assert not rig.hub.busy
    assert rig.store.data["protection_state"] == "uncertain"
    hub = await rig.create()
    hub.notify()
    await settle(hub.hold_open_timeout)
    hub.transport.control_node02.assert_not_called()


async def test_write_ahead_failure_rejects_control_and_post_ack_failure_is_not_recoverable(
    rig, caplog
):
    rig.store.fail = True
    with pytest.raises(HomeAssistantError, match="not sent"):
        await rig.hub.set_hold_open(True)
    rig.hub.transport.control_node02.assert_not_called()
    assert not rig.manager.restore_safe
    assert "persistence failed" in caplog.text
    rig.store.fail = False

    async def ack_then_disk_fails(_action):
        assert rig.store.data["protection_state"] == "uncertain"
        rig.store.fail = True
        return 4

    rig.hub.transport.control_node02.side_effect = ack_then_disk_fails
    await rig.hub.set_hold_open(True)
    assert rig.manager.status == "active" and not rig.manager.restore_safe
    assert rig.store.data["protection_state"] == "uncertain"
    # Simulate a crash: do not flush the live state before loading its durable record.
    hub = await rig.create()
    assert hub.hold_open_timeout.status == "uncertain"
    hub.notify()
    hub.transport.control_node02.assert_not_called()


async def test_crash_during_command_and_cancel_before_send(rig):
    await rig.hub.set_hold_open(True)
    # Simulate loss after the write-ahead record, before any confirmed result.
    await rig.manager.async_prepare()
    hub = await rig.create()
    assert hub.hold_open_timeout.status == "uncertain"
    rig.clock.advance(minutes=60)
    hub.notify()
    await settle(hub.hold_open_timeout)
    hub.transport.control_node02.assert_not_called()
    await rig.manager.async_complete("not_sent", enabled=True)


@pytest.mark.parametrize("minutes", [0, 1441, 1.5, True, float("nan")])
async def test_invalid_minutes(rig, minutes):
    with pytest.raises(HomeAssistantError):
        await rig.manager.async_configure(minutes=minutes)
    assert rig.manager.minutes == 30


async def test_settings_persist_and_config_entries_are_isolated(rig):
    await rig.manager.async_configure(minutes=42, enabled=False)
    hub = await rig.create()
    assert hub.hold_open_timeout.minutes == 42
    assert not hub.hold_open_timeout.enabled
    other = await rig.create(storage=MemoryStore())
    assert other.hold_open_timeout.minutes == 30
    assert other.hold_open_timeout.enabled
    assert hub.hold_open_timeout._store is not other.hold_open_timeout._store


@pytest.mark.parametrize(
    "bad",
    [
        [],
        {"timeout_enabled": "yes"},
        {
            "timeout_enabled": True,
            "timeout_minutes": 30,
            "hold_open_started_at": "2026-11-24T00:00:00",
            "hold_open_deadline": "2026-11-24T00:30:00",
            "protection_state": "active",
        },
    ],
)
async def test_corrupt_storage_does_not_create_confirmed_session(rig, bad):
    rig.store.data = bad
    hub = await rig.create()
    assert hub.hold_open_timeout.status == "uncertain"
    assert not hub.hold_open_timeout.restore_safe
    hub.notify()
    hub.transport.control_node02.assert_not_called()


@pytest.mark.usefixtures("enable_custom_integrations", "socket_enabled")
async def test_native_entities_services_store_reload_and_device(hass):
    from tests.test_ha import gateway, next_item, setup_entry

    # Use real Store and native HA service validation, with no door commands.
    with patch.object(Hub, "start_polling", lambda _hub: None):
        async with gateway() as (port, connections, received):
            entry = await setup_entry(hass, port)
            await next_item(connections)
            registry = er.async_get(hass)

            def entity(domain, suffix):
                return registry.async_get_entity_id(
                    domain, DOMAIN, f"{entry.entry_id}:2:{suffix}"
                )

            switch = entity("switch", "hold_open_timeout_protection")
            number = entity("number", "hold_open_timeout")
            hold = entity("switch", "hold_open")
            assert (
                registry.async_get(switch).device_id
                == registry.async_get(hold).device_id
            )
            assert (
                registry.async_get(number).device_id
                == registry.async_get(hold).device_id
            )
            assert registry.async_get(number).entity_category.value == "config"
            assert registry.async_get(switch).entity_category.value == "config"
            assert hass.states.get(switch).state == "on"
            state = hass.states.get(number)
            assert float(state.state) == 30
            assert {
                k: state.attributes[k]
                for k in ("min", "max", "step", "mode", "unit_of_measurement")
            } == {
                "min": 1,
                "max": 1440,
                "step": 1,
                "mode": "box",
                "unit_of_measurement": "min",
            }
            await hass.services.async_call(
                "number", "set_value", {"entity_id": number, "value": 7}, blocking=True
            )
            await hass.services.async_call(
                "switch", "turn_off", {"entity_id": switch}, blocking=True
            )
            manager = entry.runtime_data.hold_open_timeout
            assert manager.minutes == 7 and not manager.enabled
            assert received.empty()
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()
            assert await next_item(received) == b""
            await next_item(connections)
            assert manager._closed and manager._timer is None and manager._task is None
            assert hass.states.get(switch).state == "off"
            assert float(hass.states.get(number).state) == 7
            assert await hass.config_entries.async_unload(entry.entry_id)
            assert await next_item(received) == b""


@pytest.mark.usefixtures("real_store_io")
async def test_real_store_swallowed_write_error_is_detected(hass, tmp_path, caplog):
    from homeassistant.util.file import WriteError

    from custom_components.soyal_access.hold_open_timeout import ProtectionStore

    hass.config.config_dir = str(tmp_path)
    store = ProtectionStore(
        hass, 1, "soyal_access.test.hold_open_timeout", atomic_writes=True
    )
    with (
        patch.object(Store, "_async_write_data", side_effect=WriteError("disk full")),
        pytest.raises(HomeAssistantError, match="did not complete"),
    ):
        await store.async_save({"protection_state": "uncertain"})
    assert "Error writing config" in caplog.text
    await store.async_save({"protection_state": "uncertain"})
    assert await store.async_load() == {"protection_state": "uncertain"}


@pytest.mark.usefixtures("real_store_io")
async def test_real_store_shutdown_flushes_before_returning(hass, tmp_path):
    import json

    from homeassistant.core import CoreState

    from custom_components.soyal_access.hold_open_timeout import ProtectionStore

    hass.config.config_dir = str(tmp_path)
    store = ProtectionStore(
        hass, 1, "soyal_access.stop.hold_open_timeout", atomic_writes=True
    )
    hass.set_state(CoreState.stopping)
    try:
        await store.async_save({"protection_state": "active"})
        raw = await hass.async_add_executor_job(Path(store.path).read_text)
        assert json.loads(raw)["data"]["protection_state"] == "active"
        assert store._unsub_final_write_listener is None
    finally:
        hass.set_state(CoreState.not_running)


@pytest.mark.usefixtures("real_store_io")
async def test_real_store_corrupt_json_is_not_a_clean_start(hass, tmp_path):
    from custom_components.soyal_access.hold_open_timeout import ProtectionStore

    hass.config.config_dir = str(tmp_path)
    store = ProtectionStore(hass, 1, "soyal_access.corrupt.hold_open_timeout")

    def corrupt():
        Path(store.path).parent.mkdir(parents=True, exist_ok=True)
        Path(store.path).write_text("{broken")

    await hass.async_add_executor_job(corrupt)
    with pytest.raises(HomeAssistantError, match="could not be restored"):
        await store.async_load()


async def test_real_ha_timer_releases_once_with_simulated_time(hass, freezer):
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    entry = MockConfigEntry(domain=DOMAIN, data={"host": "127.0.0.1", "port": 502})
    hub = Hub(hass, entry, "gateway-device-id")
    hub.transport = SimpleNamespace(
        connected=True,
        tainted=False,
        control_node02=AsyncMock(return_value=4),
        close=AsyncMock(),
    )
    hub._validation_polls = 2
    await hub.hold_open_timeout.async_load()
    await hub.set_hold_open(True)
    freezer.tick(timedelta(minutes=30))
    async_fire_time_changed(hass, dt_util.utcnow())
    await asyncio.sleep(0)
    await settle(hub.hold_open_timeout)
    assert hub.transport.control_node02.call_count == 2
    assert hub.hold_open_timeout.status == "idle"
    assert hub.hold_open_timeout._timer is None
    await hub.stop()


async def test_cancel_during_write_ahead_keeps_previous_confirmed_session(rig):
    await rig.hub.set_hold_open(True)
    original = rig.manager.deadline
    saving = asyncio.Event()

    async def interrupted_save(_data):
        saving.set()
        await asyncio.Event().wait()

    with patch.object(rig.store, "async_save", side_effect=interrupted_save):
        task = asyncio.create_task(rig.hub.set_hold_open(False))
        await saving.wait()
        task.cancel()
        # The completion save is allowed to finish after the interrupted write.
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not rig.hub.busy
    assert rig.manager.status == "active" and rig.manager.deadline == original
    assert rig.hub.transport.control_node02.call_count == 1
    assert rig.store.data["protection_state"] == "active"
