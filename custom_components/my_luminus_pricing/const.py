"""Constants for our integration."""

DOMAIN = "my_luminus_pricing"

DEFAULT_SCAN_INTERVAL = 43200
MIN_SCAN_INTERVAL = 43200
HTTP_TIMEOUT = 60
GAS_M3_TO_KWH = 11.5

FORCAST_DATA_ELECTRICITY = {
    10: 98,  # January
    11: 94,  # February...
    12: 89,
    1: 84,
    2: 82,
    3: 80,
    4: 80,
    5: 80,
    6: 82,
    7: 87,
    8: 94,
    9: 98
}

FORCAST_DATA_GAS = {
    10: 269,  # January
    11: 116,  # February...
    12: 99,
    1: 25,
    2: 20,
    3: 20,
    4: 20,
    5: 20,
    6: 20,
    7: 20,
    8: 139,
    9: 248
}