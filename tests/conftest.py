"""Small compatibility fixes for the installed Home Assistant pytest plugin."""

import asyncio

import pytest


@pytest.fixture(autouse=True)
def enable_event_loop_debug():
    """Recreate the default loop after pytest-asyncio closes it on Python 3.13."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    loop.set_debug(True)
