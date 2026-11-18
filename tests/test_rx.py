"""Fixed RX vectors and opt-in verification of private field captures."""

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
# The last three are synthetic for the requested lengths, with no real card data.
RX_VECTORS = (
    ("io_01_idle", 11, 0x03, 1, "7E 09 00 03 01 A0 00 00 00 5D 01"),
    ("io_01_unlocked", 11, 0x03, 1, "7E 09 00 03 01 A0 40 00 00 1D 01"),
    ("io_02_idle", 11, 0x03, 2, "7E 09 00 03 02 A1 00 00 00 5F 05"),
    ("io_02_unlocked", 11, 0x03, 2, "7E 09 00 03 02 A1 40 00 00 1F 05"),
    ("event_remove_ack", 12, 0x04, 2, "7E 0A 00 04 02 A2 00 00 00 00 5B 03"),
    ("parameter_nack", 12, 0x05, 2, "7E 0A 00 05 02 A3 00 00 00 00 5B 05"),
    ("rtc", 19, 0x03, 2, "7E 11 00 03 02 01 02 03 04 05 06 07 08 09 0A 0B 0C F2 45"),
    (
        "event",
        31,
        0x0B,
        2,
        (
            "7E 1D 00 0B 02 01 02 03 04 05 06 07 00 00 01 00 "
            "00 00 00 00 00 00 00 00 00 00 00 00 00 F7 21"
        ),
    ),
    (
        "hosting",
        38,
        0x09,
        2,
        (
            "7E 24 00 09 02 00 40 00 00 00 00 00 00 00 00 00 00 00 00 "
            "00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 B4 FF"
        ),
    ),
    ("personnel", 15, 0x03, 2, "7E 0D 00 03 02 00 00 00 00 A1 A2 A3 A4 FA 89"),
)

RX_FORMAT = ReplyFormat(frozenset({0x03, 0x09, 0x0B}))
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
    return frame


@pytest.mark.parametrize(
    ("label", "size", "code", "station", "hex_bytes"),
    [pytest.param(*case, id=case[0]) for case in RX_VECTORS],
)
def test_public_rx(label, size, code, station, hex_bytes):
    raw = bytes.fromhex(hex_bytes)
    frame = verify_rx(raw, size, code, station)
    parser = FrameBuffer()
    assert parser.feed(raw[:-2]) == []
    assert parser.feed(raw[-2:] + raw) == [frame, frame]
    if label.startswith("io_"):
        assert bool(raw[6] & 0x40) == label.endswith("_unlocked")


def test_private_field_rx():
    path = os.environ.get("SOYAL_FIELD_RX")
    if path is None:
        pytest.skip("Set SOYAL_FIELD_RX to verify the seven local field captures")
    lines = Path(path).read_text(encoding="ascii").splitlines()
    assert len(lines) == 7
    for line, (label, size, code, station, _) in zip(
        lines, RX_VECTORS[:7], strict=True
    ):
        # Suppress raw data in error output, including pytest assertion diffs.
        try:
            raw = bytes.fromhex(line)
            verify_rx(raw, size, code, station)
            if label.startswith("io_") and (
                bool(raw[6] & 0x40) != label.endswith("_unlocked")
            ):
                raise ValueError("I/O output bit mismatch")
        except (ValueError, AssertionError):
            raise RuntimeError(f"Private RX verification failed for {label}") from None


@pytest.mark.parametrize("code", (0x03, 0x04, 0x05, 0x09))
def test_zero_source_is_unidentified(code):
    assert RX_FORMAT.classify(Frame(0, code, b"\x00")).source is None
