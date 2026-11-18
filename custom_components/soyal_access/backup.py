"""Quiesce event stores during HA Core configuration backups."""

import asyncio

from .const import DOMAIN


def backup_lock(hass):
    return hass.data.setdefault(f"{DOMAIN}_backup_lock", asyncio.Lock())


async def async_pre_backup(hass):
    await backup_lock(hass).acquire()
    hass.data[f"{DOMAIN}_backup_active"] = True


async def async_post_backup(hass):
    lock = backup_lock(hass)
    if hass.data.pop(f"{DOMAIN}_backup_active", False):
        lock.release()
