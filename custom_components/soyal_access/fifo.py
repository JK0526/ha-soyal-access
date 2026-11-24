"""Parsing helpers for the observed SOYAL FIFO event format."""

from datetime import datetime

from .protocol import Frame, ProtocolError, encode

EVENT_TYPES = {
    0x01: "invalid_user_pin",
    0x0B: "card_access",
    0x10: "egress",
    0x1C: "pin_access",
}


def parse_event(frame: Frame, node_id: int) -> dict:
    """Return the confirmed fields from a 31-byte FIFO event."""
    data = frame.payload
    if frame.node != 0 or len(data) != 25 or data[0] != node_id or frame.code in (4, 5):
        raise ProtocolError("Unexpected FIFO event format")

    second, minute, hour, _, day, month, year = data[1:8]
    try:
        if year > 99:
            raise ValueError("RTC year outside the supported range")
        event_time = datetime(2000 + year, month, day, hour, minute, second)
    except ValueError:
        event_time = None

    return {
        "event_code": frame.code,
        "event_type": EVENT_TYPES.get(frame.code, "unknown_event"),
        "node_id": data[0],
        "event_time_local": event_time.isoformat() if event_time else None,
        "port": data[8],
        "user_address": int.from_bytes(data[9:11], "big"),
        "raw_frame_hex": encode(frame).hex(" ").upper(),
    }
