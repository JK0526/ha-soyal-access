"""Fixed RX vectors and opt-in verification of private field captures."""

import asyncio
import os
from pathlib import Path

import pytest

from custom_components.soyal_access.protocol import (
    Frame,
    FrameBuffer,
    ReplyFormat,
    ReplyKind,
    checksums,
    decode,
    encode,
)

# The first seven vectors preserve the field cases' structure with replaced payloads.

RX_VECTORS = (
    ("io_01_idle", 11, 0x03, 1, "7E 09 00 03 01 A0 00 00 00 5D 01"),
    ("io_01_unlocked", 11, 0x03, 1, "7E 09 00 03 01 A0 40 00 00 1D 01"),
    ("io_02_idle", 11, 0x03, 2, "7E 09 00 03 02 A1 00 00 00 5F 05"),
    ("io_02_unlocked", 11, 0x03, 2, "7E 09 00 03 02 A1 40 00 00 1F 05"),
    ("event_remove_ack", 12, 0x04, 2, "7E 0A 00 04 02 A2 00 00 00 00 5B 03"),
    ("parameter_nack", 12, 0x05, 2, "7E 0A 00 05 02 A3 00 00 00 00 5B 05"),
    ("rtc", 19, 0x03, 2, "7E 11 00 03 02 01 02 03 04 05 06 07 08 09 0A 0B 0C F2 45"),
)

FINAL_RX_VECTORS = (
    (
        "event_egress",
        31,
        0x10,
        2,
        "7E 1D 00 10 02 00 00 00 00 00 00 00 00 FF FF 00 00 00 00 00 00 00 00 00 00 00 00 00 00 ED FD",
    ),
    (
        "event_card",
        31,
        0x0B,
        1,
        "7E 1D 00 0B 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 F5 01",
    ),
    (
        "hosting_locked",
        38,
        0x09,
        2,
        "7E 24 00 09 02 00 00 00 00 00 10 02 00 00 00 00 00 00 00 00 FF FF 00 00 00 00 00 00 00 00 00 00 00 00 00 00 E6 01",
    ),
    (
        "hosting_unlocked",
        38,
        0x09,
        2,
        "7E 24 00 09 02 00 40 00 00 00 10 02 00 00 00 00 00 00 00 00 FF FF 00 00 00 00 00 00 00 00 00 00 00 00 00 00 A6 01",
    ),
    ("personnel_a", 15, 0x03, 2, "7E 0D 00 03 02 00 00 00 00 00 00 00 00 FE 03"),
    ("personnel_b", 15, 0x03, 2, "7E 0D 00 03 02 00 01 02 03 04 05 06 07 FE 1F"),
)

RX_FORMAT = ReplyFormat(frozenset({0x03, 0x09, 0x0B, 0x10}))
KINDS = {0x04: ReplyKind.ACK, 0x05: ReplyKind.NACK}


def verify_rx(raw, size, code, station):
    assert len(raw) == size
    assert raw[0] == 0x7E
    assert raw[1] == size - 2
    assert raw[2:5] == bytes((0, code, station))
    xor, total = checksums(raw[2:-2])
    assert xor == raw[-2]
    assert total == raw[-1]
    frame = decode(raw)
    assert frame == Frame(0, code, raw[4:-2])
    assert encode(frame) == raw

    reply = RX_FORMAT.classify(frame)
    assert reply.kind == KINDS.get(code, ReplyKind.DATA)
    assert reply.source == station
    assert reply.frame == frame
    for split in range(1, len(raw)):
        parser = FrameBuffer()
        assert parser.feed(raw[:split]) == []
        assert parser.feed(raw[split:] + raw) == [frame, frame]
    return frame


@pytest.mark.parametrize(
    ("label", "size", "code", "station", "hex_bytes"),
    [pytest.param(*case, id=case[0]) for case in (*RX_VECTORS, *FINAL_RX_VECTORS)],
)
def test_public_rx(label, size, code, station, hex_bytes):
    raw = bytes.fromhex(hex_bytes)
    verify_rx(raw, size, code, station)
    if label.startswith("io_"):
        assert bool(raw[6] & 0x40) == label.endswith("_unlocked")


def verify_hosting_event(captures):
    egress, _, locked, unlocked, _, _ = captures
    assert locked[6] & 0x40 == 0
    assert unlocked[6] & 0x40 == 0x40
    assert locked[10:-2] == unlocked[10:-2] == egress[3:-2]


def test_public_hosting_event():
    verify_hosting_event([bytes.fromhex(case[-1]) for case in FINAL_RX_VECTORS])


@pytest.mark.usefixtures("socket_enabled")
def test_mixed_rx_over_loopback_tcp():
    packets = [bytes.fromhex(case[-1]) for case in FINAL_RX_VECTORS]

    async def exchange():
        async def send_rx(reader, writer):
            try:
                writer.write(packets[0][:7])
                await writer.drain()
                await reader.readexactly(1)
                writer.write(packets[0][7:] + b"".join(packets[1:]))
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        async with await asyncio.start_server(send_rx, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                parser = FrameBuffer()
                assert parser.feed(await reader.readexactly(7)) == []
                # Test synchronization byte, consumed locally by the simulator.
                writer.write(b"\x00")
                await writer.drain()
                assert parser.feed(await reader.read()) == [
                    decode(raw) for raw in packets
                ]
            finally:
                writer.close()
                await writer.wait_closed()

    asyncio.run(asyncio.wait_for(exchange(), 2))


@pytest.mark.parametrize(
    ("variable", "vectors"),
    (("SOYAL_FIELD_RX", RX_VECTORS), ("SOYAL_FINAL_RX", FINAL_RX_VECTORS)),
)
def test_private_field_rx(variable, vectors):
    path = os.environ.get(variable)
    if path is None:
        pytest.skip(f"Set {variable} to verify local field captures")
    lines = Path(path).read_text(encoding="ascii").splitlines()
    assert len(lines) == len(vectors)
    captures = []
    for line, (label, size, code, station, _) in zip(lines, vectors, strict=True):
        try:
            raw = bytes.fromhex(line)
            verify_rx(raw, size, code, station)
            if label.startswith("io_"):
                assert bool(raw[6] & 0x40) == label.endswith("_unlocked")
            captures.append(raw)
        except (ValueError, AssertionError):
            raise RuntimeError(f"Private RX verification failed for {label}") from None
    if variable == "SOYAL_FINAL_RX":
        try:
            verify_hosting_event(captures)
        except AssertionError:
            raise RuntimeError("Private Hosting event or Bit 6 mismatch") from None


@pytest.mark.parametrize("code", (0x03, 0x04, 0x05, 0x09))
def test_zero_source_is_unidentified(code):
    assert RX_FORMAT.classify(Frame(0, code, b"\x00")).source is None
