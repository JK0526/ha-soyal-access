import unittest

from custom_components.soyal_access.protocol import (
    Frame, FrameBuffer, ProtocolError, ReplyFormat, ReplyKind, decode, encode,
)
from tests.fixtures import FIELD_REQUESTS, FIELD_STATUS_PREFIXES


class ProtocolTests(unittest.TestCase):
    def test_field_request_vectors(self):
        for node, code, payload, raw in FIELD_REQUESTS:
            with self.subTest(raw=raw):
                frame = Frame(node, code, bytes.fromhex(payload))
                self.assertEqual(encode(frame), bytes.fromhex(raw))
                self.assertEqual(decode(bytes.fromhex(raw)), frame)

    def test_incomplete_field_status_and_synthetic_extension(self):
        fmt = ReplyFormat(frozenset({0x09}), min_data_size=2)
        for raw, station, unlocked in FIELD_STATUS_PREFIXES:
            with self.subTest(raw=raw):
                prefix = bytes.fromhex(raw)
                self.assertEqual(FrameBuffer().feed(prefix), [])
                with self.assertRaises(ProtocolError):
                    decode(prefix)
                payload = prefix[4:] + bytes(32 - len(prefix[4:]))
                full = encode(Frame(0, 9, payload))
                self.assertTrue(full.startswith(prefix))
                reply = fmt.classify(decode(full))
                self.assertEqual(reply.source, station)
                self.assertEqual(bool(reply.frame.payload[2] & 0x40), unlocked)

    def test_every_split_and_coalescing(self):
        frame = Frame(10, 20, b"\x7eembedded")
        raw = encode(frame)
        for split in range(len(raw)):
            with self.subTest(split=split):
                parser = FrameBuffer()
                self.assertEqual(parser.feed(b"noise" + raw[:split]), [])
                self.assertEqual(parser.feed(raw[split:] + raw + raw[:1]), [frame, frame])
                self.assertEqual(parser.feed(raw[1:]), [frame])

    def test_invalid_frames(self):
        raw = encode(Frame(1, 0x25))
        for malformed in (raw[:-1], b"\x00" + raw[1:], raw + b"\x00",
                          raw[:-2] + bytes([raw[-2] ^ 1]) + raw[-1:],
                          raw[:-1] + bytes([raw[-1] ^ 1])):
            with self.subTest(malformed=malformed), self.assertRaises(ProtocolError):
                decode(malformed)
        with self.assertRaises(ProtocolError):
            FrameBuffer().feed(b"\x7e\x03")

    def test_maximum_payload(self):
        frame = Frame(255, 255, bytes(range(251)))
        self.assertEqual(decode(encode(frame)), frame)
        with self.assertRaises(ProtocolError):
            encode(Frame(1, 1, bytes(252)))

    def test_reply_formats_and_variable_lengths(self):
        fmt = ReplyFormat(frozenset({0x0B}), min_data_size=10)
        for size in (25, 34):
            with self.subTest(payload_size=size - 1):
                raw = encode(Frame(0, 0x0B, b"\x02" + bytes(size - 1)))
                self.assertEqual(len(raw), size + 6)
                self.assertEqual(fmt.classify(decode(raw)).kind, ReplyKind.DATA)
        with self.assertRaises(ProtocolError):
            fmt.classify(Frame(0, 0x0B, bytes(10)))
        self.assertIsNone(fmt.classify(Frame(2, 0x0B, b"\x02")))
        self.assertIsNone(fmt.classify(Frame(0, 0x03, b"\x02")))
        for code, kind in ((4, ReplyKind.ACK), (5, ReplyKind.NACK)):
            for payload, source in ((b"", None), (b"\x02", 2), (b"\x02\x85", 2)):
                reply = fmt.classify(Frame(0, code, payload))
                self.assertEqual((reply.kind, reply.source), (kind, source))
