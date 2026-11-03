"""Constants for our integration."""

DOMAIN = "my_luminus_pricing"

DEFAULT_SCAN_INTERVAL = 43200
MIN_SCAN_INTERVAL = 43200
HTTP_TIMEOUT = 60
GAS_M3_TO_KWH = 11.5

FORCAST_DATA_ELECTRICITY = {
    10: 98,  # January
    11: 94,  # February...
    12: 89,  # March
    1: 84,   # April
    2: 88,   # May
    3: 94,   # June
    4: 110,  # July
    5: 117,  # August
    6: 90,   # September
    7: 104,  # October
    8: 94,   # November
    9: 98    # December
}

FORCAST_DATA_GAS = {
    10: 269,  # January
    11: 116,  # February...
    12: 99,
    1: 25,
    2: 14,
    3: 2,
    4: 1,
    5: 1,
    6: 2,
    7: 51,
    8: 139,
    9: 248
}

# Cookie header of a logged-in My Luminus browser session (www.luminus.be),
# format "name=value; name=value"
CONF_COOKIES = "cookies"

# Load a My Luminus page this often to renew the 24 h session cookies
KEEPALIVE_INTERVAL_HOURS = 6
