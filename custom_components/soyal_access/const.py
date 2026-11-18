"""Integration constants."""

DOMAIN = "soyal_access"
PLATFORMS = ("button", "switch", "binary_sensor", "sensor")
DEFAULTS = {
    "port": 502,
    "mode": "raw_tcp",
    "stations": "1,2",
    "timeout": 2.0,
    "reconnect_delay": 1.0,
    "enable_control": False,
}
