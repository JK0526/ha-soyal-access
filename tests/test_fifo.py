"""Offline FIFO and crash-boundary tests using a real loopback TCP socket."""

import asyncio
import io
import sqlite3
import tarfile
from collections import Counter
from contextlib import asynccontextmanager, closing
from types import SimpleNamespace

import pytest

from custom_components.soyal_access.client import BusClient
from custom_components.soyal_access.event_store import EventStore
from custom_components.soyal_access.event_sync import FifoSync, SyncStopped
from custom_components.soyal_access.protocol import Frame, decode, encode

pytestmark = pytest.mark.asyncio


class Peer:
    def __init__(self):
        self.removed = Counter()
        self.fault = None

    def event(self, node):
        number = self.removed[node]
        code = (0x10, 0x0B, 0x1C)[number % 3]
        return encode(
            Frame(
                0,
                code,
                bytes([node])
                + bytes((5, 4, 3, 2, 1, 11, 26, 7))
                + number.to_bytes(2, "big")
                + bytes(14),
            )
        )

    async def respond(self, reader, writer):
        try:
            header = await reader.readexactly(2)
            request = decode(header + await reader.readexactly(header[1]))
            node = request.node
            if request.code == 0x37:
                if self.fault == "nack":
                    raw = encode(Frame(0, 5, bytes([node])))
                else:
                    self.removed[node] += 1
                    if self.fault == "lost_ack":
                        return
                    raw = encode(Frame(0, 4, bytes([node])))
            else:
                assert request.code == 0x25
                raw = self.event(node)
                if self.fault == "identical":
                    raw = encode(
                        Frame(
                            0, 0x10, bytes([node, 5, 4, 3, 2, 1, 11, 26, 7]) + bytes(16)
                        )
                    )
                elif self.fault == "foreign":
                    raw = self.event(3)
                elif self.fault == "checksum":
                    raw = raw[:-1] + bytes([raw[-1] ^ 1])
                elif self.fault == "truncated":
                    raw = raw[:10]
                elif self.fault == "unclassified_ack":
                    raw = encode(Frame(0, 4, bytes([node])))
            writer.write(raw)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


@asynccontextmanager
async def rig(directory, allow=True):
    peer = Peer()
    server = await asyncio.start_server(peer.respond, "127.0.0.1", 0)
    async with (
        server,
        BusClient(
            "127.0.0.1", server.sockets[0].getsockname()[1], {1, 2}, simulate_fifo=True
        ) as client,
    ):
        store = EventStore(directory / "gateway.sqlite3", "gateway")
        await store.open()
        try:
            yield peer, FifoSync(store, client, allow_remove=allow)
        finally:
            await store.close()


@pytest.mark.parametrize("size", [1, 1000, 10000])
async def test_streamed_backfill(tmp_path, size):
    async with rig(tmp_path) as (peer, sync):
        if size == 10000:
            with pytest.raises(ValueError, match="loopback"):
                BusClient(
                    "example.invalid", 502, {1}, enable_control=True, simulate_fifo=True
                )
        remaining = size
        while remaining:
            count = min(64, remaining)
            assert await sync.batch(2, limit=count, interval=0) == count
            remaining -= count
        state = await sync.store.state(2)
        assert state["saved"] == state["processed"] == peer.removed[2] == size
        assert state["phase"] == "idle" and state["last_success"]
        with closing(sqlite3.connect(sync.store.path)) as db:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM access_events WHERE status='confirmed'"
                ).fetchone()[0]
                == size
            )


@pytest.mark.parametrize(
    ("fault", "removed"),
    [
        ("lost_ack", 1),
        ("nack", 0),
        ("identical", 1),
        ("unclassified_ack", 0),
        ("foreign", 0),
        ("checksum", 0),
        ("truncated", 0),
    ],
)
async def test_uncertain_never_replays(tmp_path, fault, removed):
    async with rig(tmp_path) as (peer, sync):
        peer.fault = fault
        with pytest.raises(SyncStopped):
            await sync.batch(1, limit=1)
        peer.fault = None
        with pytest.raises(SyncStopped):
            await sync.batch(1, limit=1)
        assert peer.removed[1] == removed
        assert (await sync.store.state(1))["phase"] == "uncertain"
        assert await sync.batch(2, limit=1, interval=0) == 1


async def test_two_stations(tmp_path):
    async with rig(tmp_path) as (peer, sync):
        assert await asyncio.gather(
            *(sync.batch(n, limit=3, interval=0) for n in (1, 2))
        ) == [3, 3]
        assert peer.removed == {1: 3, 2: 3}


@pytest.mark.parametrize("phase", ["ready_to_delete", "delete_in_flight"])
async def test_restart_after_crash(tmp_path, phase):
    async with rig(tmp_path, allow=False) as (peer, sync):
        await sync.batch(1, limit=1)
        assert not peer.removed
        if phase == "delete_in_flight":
            await sync.store.mark_sent(1, peer.event(1))
            peer.removed[1] += 1
        assert (await sync.store.state(1))["phase"] == phase
    async with rig(tmp_path) as (peer, sync):
        assert (await sync.store.state(1))["phase"] == "uncertain"
        with pytest.raises(SyncStopped):
            await sync.batch(1)
        assert not peer.removed


@pytest.mark.parametrize("fault", ["insert", "commit"])
async def test_sqlite_failure(tmp_path, fault):
    async with rig(tmp_path) as (peer, sync):
        saving = False

        def authorize(action, arg, _table, _database, _trigger):
            nonlocal saving
            if action == sqlite3.SQLITE_INSERT and arg == "access_events":
                saving = True
                if fault == "insert":
                    return sqlite3.SQLITE_DENY
            if (
                fault == "commit"
                and saving
                and action == sqlite3.SQLITE_TRANSACTION
                and arg == "COMMIT"
            ):
                saving = False
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        await sync.store._run(sync.store._db.set_authorizer, authorize)
        with pytest.raises(SyncStopped):
            await sync.batch(1)
        assert not peer.removed
        assert (await sync.store.state(1))["saved"] == 0


async def test_sqlite_and_ha_backup_restore(tmp_path):
    from homeassistant.components.backup.manager import CoreBackupReaderWriter

    config = tmp_path / "config"
    directory = config / "soyal_access"
    restored = tmp_path / "restored"
    restored.mkdir()
    async with rig(directory) as (_, sync):
        await sync.batch(2, limit=2, interval=0)
        await sync.store.backup(restored / "gateway.sqlite3")
        instance = SimpleNamespace(
            _hass=SimpleNamespace(config=SimpleNamespace(path=lambda: str(config)))
        )
        archive = tmp_path / "ha.tar"
        await asyncio.to_thread(
            CoreBackupReaderWriter._mkdir_and_generate_backup_contents,
            instance,
            {},
            False,
            None,
            archive,
        )
        with tarfile.open(archive) as outer:
            inner = io.BytesIO(
                outer.extractfile(
                    next(
                        n
                        for n in outer.getnames()
                        if n.endswith("homeassistant.tar.gz")
                    )
                ).read()
            )
            with tarfile.open(fileobj=inner) as core:
                name = next(
                    n
                    for n in core.getnames()
                    if n.endswith("/soyal_access/gateway.sqlite3")
                )
                archived = tmp_path / "archived.sqlite3"
                archived.write_bytes(core.extractfile(name).read())
        with closing(sqlite3.connect(archived)) as db:
            assert db.execute("SELECT processed FROM sync_state").fetchone()[0] == 2
            assert db.execute("SELECT COUNT(*) FROM access_events").fetchone()[0] == 2
    async with rig(restored) as (peer, sync):
        with pytest.raises(SyncStopped, match="checkpoint_mismatch"):
            await sync.batch(2)
        assert not peer.removed


async def test_review_restore_and_no_overwrite(tmp_path):
    from custom_components.soyal_access.event_store import restore_for_review

    async with rig(tmp_path) as (_, sync):
        await sync.batch(1, limit=1, interval=0)
        snapshot = tmp_path / "snapshot"
        restored = tmp_path / "review"
        await sync.store.backup(snapshot)
        await restore_for_review(snapshot, restored)
        with closing(sqlite3.connect(restored)) as db:
            assert db.execute("SELECT phase,processed FROM sync_state").fetchone() == (
                "paused",
                1,
            )
        with pytest.raises(FileExistsError):
            await restore_for_review(snapshot, restored)


async def test_cancel_after_delete_then_reopen(tmp_path):
    async with rig(tmp_path) as (peer, sync):
        entered = asyncio.Event()

        async def interrupted_advance(*args):
            entered.set()
            await asyncio.Future()

        from unittest.mock import patch

        with patch.object(sync.store, "advance", interrupted_advance):
            task = asyncio.create_task(sync.batch(1, limit=1))
            await asyncio.wait_for(entered.wait(), 2)
            await sync.close()
            assert task.cancelled()
        assert peer.removed[1] == 1
        assert (await sync.store.state(1))["possibly_sent"] == 1
        with pytest.raises(SyncStopped, match="closed"):
            await sync.batch(1)
    async with rig(tmp_path) as (peer, sync):
        assert (await sync.store.state(1))["phase"] == "uncertain"
        with pytest.raises(SyncStopped):
            await sync.batch(1)
        assert peer.removed[1] == 0


async def test_resume_at_verified_checkpoint(tmp_path):
    async with rig(tmp_path) as (_, sync):
        await sync.batch(2, limit=1, interval=0)
    async with rig(tmp_path) as (peer, sync):
        peer.removed[2] = 1
        await sync.batch(2, limit=1, interval=0)
        state = await sync.store.state(2)
        assert state["saved"] == state["processed"] == 2
        with closing(sqlite3.connect(sync.store.path)) as db:
            assert db.execute(
                "SELECT controller_time,event_type,port,user_address,origin,length(raw) FROM access_events ORDER BY id"
            ).fetchall() == [
                ("2026-11-01T03:04:05", 0x10, 7, 0, "backfill", 31),
                ("2026-11-01T03:04:05", 0x0B, 7, 1, "backfill", 31),
            ]


async def test_checkpoint_constraints(tmp_path):
    async with rig(tmp_path) as (peer, sync):
        head = peer.event(1)
        await sync.store.open()
        with pytest.raises(ValueError):
            await sync.batch(1, limit=0)
        with pytest.raises(ValueError):
            await sync.store.save_head(2, head)
        with pytest.raises(ValueError):
            await sync.store.save_head(1, head)
        with pytest.raises(ValueError):
            await sync.store.mark_sent(1, head)
        with pytest.raises(ValueError):
            await sync.store.advance(1, head, head)


async def test_simulation_honors_control_quarantine(tmp_path):
    async with rig(tmp_path) as (peer, sync):
        sync.control_uncertain = {1}
        with pytest.raises(SyncStopped, match="control_uncertain"):
            await sync.batch(1)
        assert peer.removed[1] == 0
        assert await sync.batch(2, limit=1, interval=0) == 1
