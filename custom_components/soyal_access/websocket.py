"""Admin-only Home Assistant access to local events and people."""

import sqlite3
from datetime import date, datetime

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState

from .const import DOMAIN

_STATION = vol.All(int, vol.Range(min=1, max=255))
_ADDRESS = vol.All(int, vol.Range(min=0, max=65535))
_NAME = vol.All(str, str.strip, vol.Length(min=1, max=120))
_LIMIT = vol.All(int, vol.Range(min=1, max=200))
_FILTERS = {
    vol.Optional("station"): _STATION,
    vol.Optional("user_address"): _ADDRESS,
    vol.Optional("name"): _NAME,
}


def _date(value):
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as err:
        raise vol.Invalid("Invalid calendar date") from err


def _event_cursor(value):
    if len(value) != 2 or type(value[1]) is not int or not 0 < value[1] <= 2**63 - 1:
        raise vol.Invalid("Invalid event cursor")
    try:
        timestamp = datetime.fromisoformat(value[0])
    except (ValueError, TypeError) as err:
        raise vol.Invalid("Invalid event cursor timestamp") from err
    if (
        timestamp.tzinfo is not None
        or timestamp.isoformat(timespec="seconds") != value[0]
    ):
        raise vol.Invalid("Expected controller local timestamp")
    return value


def _person_cursor(value):
    if len(value) != 2:
        raise vol.Invalid("Invalid person cursor")
    return [_STATION(value[0]), _ADDRESS(value[1])]


def async_register(hass):
    key = f"{DOMAIN}_websocket_registered"
    if hass.data.get(key):
        return
    for handler in (events, people, person_save, person_delete):
        websocket_api.async_register_command(hass, handler)
    hass.data[key] = True


def _store(hass, connection, msg):
    entry = hass.config_entries.async_get_entry(msg["entry_id"])
    if (
        entry is None
        or entry.domain != DOMAIN
        or entry.state is not ConfigEntryState.LOADED
    ):
        connection.send_error(msg["id"], "not_found", "Gateway is unavailable")
        return None
    return entry.runtime_data.events


async def _respond(connection, request_id, operation):
    try:
        result = await operation
    except sqlite3.IntegrityError:
        connection.send_error(request_id, "conflict", "Person already exists")
    except LookupError:
        connection.send_error(request_id, "not_found", "Person not found")
    except (sqlite3.Error, RuntimeError):
        connection.send_error(
            request_id, "storage_error", "Local storage is unavailable"
        )
    else:
        connection.send_result(request_id, result)


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/events",
        vol.Required("entry_id"): str,
        vol.Optional("filters", default=dict): {
            **_FILTERS,
            vol.Optional("start"): vol.All(str, _date),
            vol.Optional("end"): vol.All(str, _date),
            vol.Optional("event_type"): vol.All(int, vol.Range(min=0, max=255)),
        },
        vol.Optional("cursor"): vol.All(list, _event_cursor),
        vol.Optional("limit", default=50): _LIMIT,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def events(hass, connection, msg):
    filters = msg["filters"]
    if "start" in filters and "end" in filters and filters["start"] > filters["end"]:
        connection.send_error(msg["id"], "invalid_format", "Start must precede end")
        return
    if (store := _store(hass, connection, msg)) is not None:
        await _respond(
            connection,
            msg["id"],
            store.history_page(filters, msg.get("cursor"), msg["limit"]),
        )


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/people",
        vol.Required("entry_id"): str,
        vol.Optional("filters", default=dict): _FILTERS,
        vol.Optional("cursor"): vol.All(list, _person_cursor),
        vol.Optional("limit", default=50): _LIMIT,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def people(hass, connection, msg):
    if (store := _store(hass, connection, msg)) is not None:
        await _respond(
            connection,
            msg["id"],
            store.people_page(msg["filters"], msg.get("cursor"), msg["limit"]),
        )


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/person/save",
        vol.Required("entry_id"): str,
        vol.Required("action"): vol.In(("create", "update")),
        vol.Required("station"): _STATION,
        vol.Required("user_address"): _ADDRESS,
        vol.Required("name"): _NAME,
        vol.Optional("employee_id"): vol.All(str, str.strip, vol.Length(max=80)),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def person_save(hass, connection, msg):
    if (store := _store(hass, connection, msg)) is not None:
        await _respond(
            connection,
            msg["id"],
            store.save_person(
                msg["action"],
                msg["station"],
                msg["user_address"],
                msg["name"],
                msg.get("employee_id") or None,
            ),
        )


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/person/delete",
        vol.Required("entry_id"): str,
        vol.Required("station"): _STATION,
        vol.Required("user_address"): _ADDRESS,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def person_delete(hass, connection, msg):
    if (store := _store(hass, connection, msg)) is not None:
        await _respond(
            connection,
            msg["id"],
            store.delete_person(msg["station"], msg["user_address"]),
        )
