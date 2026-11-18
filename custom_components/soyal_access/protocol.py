"""Codec for standard 0x7E SOYAL frames."""

from dataclasses import dataclass
from enum import Enum

HEADER = 0x7E
MIN_LENGTH = 4
MAX_PAYLOAD = 251


class ProtocolError(ValueError):
    """A frame or response violates the supported wire format."""


@dataclass(frozen=True, slots=True)
class Frame:
    node: int
    code: int
    payload: bytes = b""


def checksums(body: bytes) -> bytes:
    """Calculate XOR and the low byte of the additive checksum."""
    xor = 0xFF
    for value in body:
        xor ^= value
    return bytes((xor, (sum(body) + xor) & 0xFF))


def encode(frame: Frame) -> bytes:
    if len(frame.payload) > MAX_PAYLOAD:
        raise ProtocolError("Payload exceeds the standard frame length")
    body = bytes((frame.node, frame.code)) + frame.payload
    return bytes((HEADER, len(body) + 2)) + body + checksums(body)


def decode(raw: bytes) -> Frame:
    if len(raw) < MIN_LENGTH + 2 or raw[0] != HEADER:
        raise ProtocolError("Invalid SOYAL frame header or minimum size")
    if len(raw) != raw[1] + 2:
        raise ProtocolError("SOYAL frame length mismatch")
    body = raw[2:-2]
    if checksums(body) != raw[-2:]:
        raise ProtocolError("SOYAL checksum mismatch")
    return Frame(body[0], body[1], body[2:])


class FrameBuffer:
    """Accumulate TCP chunks. Discard leading noise before a frame header."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> list[Frame]:
        self._buffer.extend(chunk)
        frames = []
        offset = 0
        while (start := self._buffer.find(HEADER, offset)) >= 0:
            offset = start
            if len(self._buffer) - start < 2:
                break
            length = self._buffer[start + 1]
            if length < MIN_LENGTH:
                raise ProtocolError("SOYAL frame length below minimum")
            end = start + length + 2
            if end > len(self._buffer):
                break
            frames.append(decode(bytes(self._buffer[start:end])))
            offset = end
        else:
            offset = len(self._buffer)
        del self._buffer[:offset]
        return frames


class ReplyKind(Enum):
    ACK = 0x04
    NACK = 0x05
    DATA = "data"


@dataclass(frozen=True, slots=True)
class Reply:
    kind: ReplyKind
    source: int | None
    frame: Frame


@dataclass(frozen=True, slots=True)
class ReplyFormat:
    """Expected data codes and minimum bytes following the source ID."""

    data_codes: frozenset[int] = frozenset()
    min_data_size: int = 0

    def classify(self, frame: Frame) -> Reply | None:
        if frame.node != 0:
            return None
        if frame.code in (ReplyKind.ACK.value, ReplyKind.NACK.value):
            source = (frame.payload[0] or None) if frame.payload else None
            return Reply(ReplyKind(frame.code), source, frame)
        if frame.code not in self.data_codes:
            return None
        if len(frame.payload) < 1 + self.min_data_size:
            raise ProtocolError("SOYAL data reply is missing required bytes")
        return Reply(ReplyKind.DATA, frame.payload[0] or None, frame)
