"""Field-supplied bytes and separately identified synthetic test data."""

# Request body excludes the framing fields and checksums.
FIELD_REQUESTS = (
    (1, 0x21, "84", "7E 05 01 21 84 5B 01"),
    (2, 0x21, "84", "7E 05 02 21 84 58 FF"),
    (1, 0x21, "82", "7E 05 01 21 82 5D 01"),
    (2, 0x21, "82", "7E 05 02 21 82 5E 03"),
    (1, 0x21, "83", "7E 05 01 21 83 5C 01"),
    (2, 0x21, "83", "7E 05 02 21 83 5F 05"),
    (1, 0x21, "00 00", "7E 06 01 21 00 00 DF 01"),
    (2, 0x21, "00 00", "7E 06 02 21 00 00 DC FF"),
    (1, 0x18, "", "7E 04 01 18 E6 FF"),
    (2, 0x18, "", "7E 04 02 18 E5 FF"),
    (1, 0x25, "", "7E 04 01 25 DB 01"),
    (2, 0x25, "", "7E 04 02 25 D8 FF"),
    (1, 0x37, "", "7E 04 01 37 C9 01"),
    (2, 0x37, "", "7E 04 02 37 CA 03"),
)

FIELD_STATUS_PREFIXES = (
    ("7E 24 00 09 02 00 10 00 18 00", 2, False),
    ("7E 24 00 09 02 00 50 00 18 00", 2, True),
    ("7E 24 00 09 01 00 50 00 18 00", 1, True),
)
